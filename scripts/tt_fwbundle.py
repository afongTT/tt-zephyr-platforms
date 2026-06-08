#!/usr/bin/env python3

# Copyright (c) 2025 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""
Tools to manage Tenstorrent firmware bundles.
Supports creating and combining firmware bundles,
as well as extracting their contents.
"""

import argparse
import hashlib
from intelhex import IntelHex
from base64 import b16encode
from pathlib import Path
from typing import Optional
import os
import sys
import shutil
import json
import tarfile
import tempfile

import tt_boot_fs


def bundle_metadata(bundle: Path, board: str = "") -> dict:
    """
    Helper to read firmware bundle file and extract JSON metadata.
    Reads manifest.json and board-specific tt-boot-fs contents.
    """
    data = {}
    try:
        with (
            tarfile.open(bundle, "r:gz") as tar,
            tempfile.TemporaryDirectory() as tempdir,
        ):
            manifest_file = tar.extractfile("./manifest.json")
            data["manifest"] = json.load(manifest_file)
            names = tar.getnames()
            image_files = [name for name in names if "image.bin" in name]
            if board:
                # Filter to specified board only
                image_files = [
                    img for img in image_files if img.startswith(f"./{board}/")
                ]
            if len(image_files) == 0:
                print(f"No images found for board '{board}' in bundle.")
                sys.exit(os.EX_DATAERR)
            for img in image_files:
                board_name = img.split("/")[1]
                board_data = {}
                tar.extract(img, path=tempdir, filter="data")
                try:
                    # Attempt to pull out tt-boot-fs data for this board

                    board_data["bootfs"] = tt_boot_fs.ls(
                        Path(tempdir) / img,
                        output_json=True,
                        input_base64=True,
                        verbose=-1,
                    )
                except Exception:
                    # Ignore errors parsing tt-boot-fs, it won't work for
                    # non-blackhole boards
                    pass
                # Compute sha256 of the bundle binary
                with open(Path(tempdir) / img, "rb") as f:
                    bundle_binary = f.read()
                    board_data["sha256"] = hashlib.sha256(bundle_binary).hexdigest()
                data[board_name] = board_data
    except KeyError as e:
        print(f"Firmware bundle missing expected file: {e}")
        sys.exit(os.EX_DATAERR)
    except FileNotFoundError:
        print(f"Firmware bundle file not found: {bundle}")
        sys.exit(os.EX_NOINPUT)
    return data


def extract_bundle_image_binary(bundle: Path, board: str) -> bytes:
    """
    Extract and expand the full board image from a firmware bundle.
    """
    try:
        with (
            tarfile.open(bundle, "r:gz") as tar,
            tempfile.TemporaryDirectory() as tempdir,
        ):
            image_path = f"./{board}/image.bin"
            tar.extract(image_path, path=tempdir, filter="data")
            return tt_boot_fs.load_bootfs_binary(
                Path(tempdir) / image_path, input_base64=True
            )
    except KeyError as e:
        print(f"Firmware bundle missing expected file: {e}")
        sys.exit(os.EX_DATAERR)
    except FileNotFoundError:
        print(f"Firmware bundle file not found: {bundle}")
        sys.exit(os.EX_NOINPUT)


def extract_bundle_binary(bundle: Path, board: str, tag: str, output: Path):
    """
    Extracts a specific binary from a firmware bundle based on board and image tag.
    """
    try:
        with (
            tarfile.open(bundle, "r:gz") as tar,
            tempfile.TemporaryDirectory() as tempdir,
        ):
            tar.extract(f"./{board}/image.bin", path=tempdir, filter="data")
            return tt_boot_fs.extract(
                Path(tempdir) / f"./{board}/image.bin", tag, output, input_base64=True
            )
    except KeyError as e:
        print(f"Firmware bundle missing expected file: {e}")
        sys.exit(os.EX_DATAERR)
    except FileNotFoundError:
        print(f"Firmware bundle file not found: {bundle}")
        sys.exit(os.EX_NOINPUT)


def ls_fw_bundle(bundle: Path, board: str = "", output_json: bool = False):
    """
    Lists the contents of a firmware bundle file.
    """
    fw_data = bundle_metadata(bundle, board)
    if not output_json:
        print(f"Firmware Bundle: {bundle}")
        manifest = fw_data["manifest"]
        bv = manifest["bundle_version"]
        print(
            f"Bundle Version: {bv['fwId']}.{bv['releaseId']}.{bv['patch']}.{bv['debug']}"
        )
        fw_data.pop("manifest")
        for board_name, fw_data in fw_data.items():
            print("\n" + "=" * 80)
            print(f"Board: {board_name}")
            print(f"Image SHA256: {fw_data['sha256']}")
            if "bootfs" not in fw_data:
                print("No tt-boot-fs data found.")
                continue
            bootfs = fw_data["bootfs"]
            print("tt-boot-fs contents:")
            hdr = "spi_addr\timage_tag\tsize\tcopy_dest\tdata_crc\tflags\t\tfd_crc\t\tdigest"
            print(hdr)
            bar = "-" * len(hdr.expandtabs())
            print(bar)
            for entry in bootfs:
                print(
                    f"{entry['spi_addr']:08x}\t{entry['image_tag']:<8}\t{entry['size']}\t"
                    f"{entry['copy_dest']:08x}\t{entry['data_crc']:08x}\t"
                    f"{entry['flags']:08x}\t{entry['fd_crc']:08x}\t{entry['digest']:.10}"
                )
    else:
        print(json.dumps(fw_data, indent=2))


def build_bootfs_digest_map(bootfs: list[dict]) -> dict[str, str]:
    """
    Builds a map of image tags to digests from a tt-boot-fs listing.
    We prefer digest if present, as these do not change when the image is
    re-signed. If digest is not present, we fall back to using data_crc.
    """
    digest_map = {}
    for entry in bootfs:
        tag = entry["image_tag"]
        digest = entry.get("digest")
        if digest and digest != "N/A":
            digest_map[tag] = digest
        else:
            # Fall back to data_crc if digest not present
            digest_map[tag] = f"crc-{entry['data_crc']:08x}"
    return digest_map


def diff_fw_bundles(bundle1: Path, bundle2: Path):
    """
    Compares two firmware bundles and lists differences.
    """
    fw_data1 = bundle_metadata(bundle1)
    fw_data2 = bundle_metadata(bundle2)

    diff_found = False
    # Compare manifests
    if fw_data1["manifest"] != fw_data2["manifest"]:
        diff_found = True
        print("Manifests differ:")
        print(f"  Bundle 1: {fw_data1['manifest']}")
        print(f"  Bundle 2: {fw_data2['manifest']}")
    # Compare boards
    boards1 = set(fw_data1.keys()) - {"manifest"}
    boards2 = set(fw_data2.keys()) - {"manifest"}
    if boards1 != boards2:
        diff_found = True
        print("Boards differ:")
        print(f"  Bundle 1 boards: {boards1}")
        print(f"  Bundle 2 boards: {boards2}")
    for board in boards1.intersection(boards2):
        board_data1 = fw_data1[board]
        board_data2 = fw_data2[board]
        # Compare tt-boot-fs contents if available
        if "bootfs" in board_data1 and "bootfs" in board_data2:
            map1 = build_bootfs_digest_map(board_data1["bootfs"])
            map2 = build_bootfs_digest_map(board_data2["bootfs"])
            if map1 != map2:
                diff_found = True
                print(f"Board {board} tt-boot-fs contents differ:")
                tags1 = set(map1.keys())
                tags2 = set(map2.keys())
                all_tags = tags1.union(tags2)
                for tag in all_tags:
                    digest1 = map1.get(tag, "<missing>")
                    digest2 = map2.get(tag, "<missing>")
                    if digest1 != digest2:
                        print(f"  Image tag '{tag}' differs:")
                        print(f"    Bundle 1 digest: {digest1}")
                        print(f"    Bundle 2 digest: {digest2}")
        elif board_data1["sha256"] != board_data2["sha256"]:
            diff_found = True
            print(f"Board {board} images differ:")
            print(f"  Bundle 1 SHA256: {board_data1['sha256']}")
            print(f"  Bundle 2 SHA256: {board_data2['sha256']}")
    if not diff_found:
        print("No differences found between the two firmware bundles.")
        return os.EX_OK
    else:
        return os.EX_DATAERR


def combine_fw_bundles(combine: list[Path], output: Path):
    """
    Combines multiple firmware bundle files into a single tar.gz file.
    """
    # Create a temporary directory to extract bundles into
    temp_dir = Path(tempfile.mkdtemp())
    # Extract each bundle into the temp directory
    try:
        for bundle in combine:
            with tarfile.open(bundle, "r:gz") as tar:
                tar.extractall(path=temp_dir, filter="data")
        # Create combined bundle
        if output.exists():
            output.unlink()
        with tarfile.open(output, "x:gz") as tar:
            tar.add(temp_dir, arcname=".")

    except Exception as e:
        raise e
    finally:
        shutil.rmtree(temp_dir)


def create_fw_bundle(output: Path, version: list[int], boot_fs: dict[str, Path] = {}):
    """
    Creates a firmware bundle tar.gz file, from tt_boot_fs images.
    """
    bundle_dir = Path(tempfile.mkdtemp())
    try:
        # Process (board, tt_boot_fs) pairs
        for board, image in boot_fs.items():
            board_dir = bundle_dir / board
            board_dir.mkdir()
            mask = [{"tag": "write-boardcfg"}]
            with open(board_dir / "mask.json", "w") as file:
                file.write(json.dumps(mask))
            mapping = []
            with open(board_dir / "mapping.json", "w") as file:
                file.write(json.dumps(mapping))
            if image.suffix == ".hex":
                # Encode offsets using @addr format tt-flash supports
                ih = IntelHex(str(image))
                b16out = ""
                for off, end in ih.segments():
                    b16out += f"@{off}\n"
                    b16out += b16encode(
                        ih.tobinarray(start=off, size=end - off)
                    ).decode("ascii")
                    b16out += "\n"
            else:
                with open(image, "rb") as img:
                    binary = img.read()
                    # Convert image to base16 encoded ascii to conform to
                    # tt-flash format
                    b16out = b16encode(binary).decode("ascii")
            with open(board_dir / "image.bin", "w") as img:
                img.write(b16out)

        # Update the manifest last, so we can specify the bundle_version
        manifest = {
            "version": "2.0.0",  # manifest file version
            "bundle_version": {
                "fwId": version[0],
                "releaseId": version[1],
                "patch": version[2],
                "debug": version[3],
            },
        }
        with open(bundle_dir / "manifest.json", "w+") as file:
            file.write(json.dumps(manifest))

        # Compress output as tar.gz
        if output.exists():
            output.unlink()
        with tarfile.open(output, "x:gz") as tar:
            tar.add(bundle_dir, arcname=".")

    except Exception as e:
        raise e
    finally:
        shutil.rmtree(bundle_dir)


def invoke_create_fw_bundle(args):
    # Convert e.g. "80.16.0.1" to [80, 16, 0, 1]
    version = args.version.split(".")
    if len(version) != 4:
        raise RuntimeError("Invalid bundle version format")
    for i in range(4):
        version[i] = int(version[i])
    setattr(args, "version", version)

    # e.g. bootfs["P100-1"] = blah/p100/tt_boot_fs.bin
    if len(args.bootfs) % 2 != 0:
        raise RuntimeError(f"Invalid number of boot fs arguments: {len(args.boot_fs)}")
    bootfs = {}
    for i in range(0, len(args.bootfs), 2):
        bootfs[args.bootfs[i]] = Path(args.bootfs[i + 1])
    setattr(args, "bootfs", bootfs)

    create_fw_bundle(args.output, args.version, args.bootfs)
    print(f"Wrote fwbundle to {args.output}")
    return os.EX_OK


def invoke_combine_fw_bundle(args):
    combine_fw_bundles(args.bundles, args.output)
    print(f"Wrote combined fwbundle to {args.output}")
    return os.EX_OK


def invoke_ls_fw_bundle(args):
    ls_fw_bundle(args.bundle, board=args.board, output_json=args.json)
    return os.EX_OK


def invoke_diff_fw_bundles(args):
    return diff_fw_bundles(args.bundle1, args.bundle2)


def invoke_extract_fw_bundle(args):
    ret = extract_bundle_binary(args.bundle, args.board, args.tag, args.output)
    if ret == os.EX_OK:
        print(f"Extracted image '{args.tag}' for board '{args.board}' to {args.output}")
    return ret


def compare_flash_to_bundle(
    flash: Path,
    bundle: Path,
    board: str,
    *,
    report: Optional[Path] = None,
    csv: Optional[Path] = None,
    diff_offsets_dir: Optional[Path] = None,
    output_json: bool = False,
) -> int:
    """
    Compare a local flash dump against a board image inside a firmware bundle.
    """
    if not flash.exists():
        print(f"Flash file not found: {flash}")
        return os.EX_NOINPUT
    if not bundle.exists():
        print(f"Firmware bundle file not found: {bundle}")
        return os.EX_NOINPUT

    actual_data = tt_boot_fs.load_bootfs_binary(flash)
    expected_data = extract_bundle_image_binary(bundle, board)
    result = tt_boot_fs.compare_bootfs_images(actual_data, expected_data)

    meta = bundle_metadata(bundle, board)
    manifest = meta.get("manifest", {})
    bv = manifest.get("bundle_version", {})
    bundle_version = (
        f"{bv.get('fwId', '?')}.{bv.get('releaseId', '?')}."
        f"{bv.get('patch', '?')}.{bv.get('debug', '?')}"
        if bv
        else "unknown"
    )

    header_lines = [
        "## Sources",
        "",
        "| Item | Path / value |",
        "|------|----------------|",
        f"| Flash dump | `{flash}` |",
        f"| Flash size | `0x{result.actual_size:X}` ({result.actual_size:,} bytes) |",
        f"| Expected bundle | `{bundle}` → `{board}/image.bin` |",
        f"| Expected expanded | `0x{result.expected_size:X}` ({result.expected_size:,} bytes) |",
        f"| Bundle version | {bundle_version} |",
        f"| Board | {board} |",
    ]

    if output_json:
        payload = tt_boot_fs.compare_result_to_json(result)
        payload["flash"] = str(flash)
        payload["bundle"] = str(bundle)
        payload["board"] = board
        payload["bundle_version"] = bundle_version
        print(json.dumps(payload, indent=2))
    else:
        tt_boot_fs.print_compare_summary(result)

    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            tt_boot_fs.format_compare_report(
                result,
                actual_label=str(flash),
                expected_label=f"{board} ({bundle})",
                header_lines=header_lines,
            ),
            encoding="utf-8",
        )
        if not output_json:
            print(f"Wrote report to {report}")

    if csv:
        tt_boot_fs.write_compare_csv(result, csv)
        if not output_json:
            print(f"Wrote CSV to {csv}")

    if diff_offsets_dir:
        tt_boot_fs.write_compare_diff_offsets(
            result, actual_data, expected_data, diff_offsets_dir
        )
        if not output_json:
            print(f"Wrote diff offsets to {diff_offsets_dir}")

    return os.EX_OK if result.match else os.EX_DATAERR


def invoke_compare_flash_to_bundle(args):
    return compare_flash_to_bundle(
        args.flash,
        args.bundle,
        args.board,
        report=args.report,
        csv=args.csv,
        diff_offsets_dir=args.diff_offsets_dir,
        output_json=args.json,
    )


def parse_args():
    """
    Parse command line arguments.
    """
    parser = argparse.ArgumentParser(
        description="Tools to manage Tenstorrent firmware bundles.",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers()

    # Create a firmware bundle
    fw_bundle_create_parser = subparsers.add_parser(
        "create", help="Create a firmware bundle"
    )
    fw_bundle_create_parser.set_defaults(func=invoke_create_fw_bundle)
    fw_bundle_create_parser.add_argument(
        "-v",
        "--version",
        metavar="VERSION",
        help="bundle version (e.g. 80.16.0.1)",
        required=True,
    )
    fw_bundle_create_parser.add_argument(
        "-o",
        "--output",
        metavar="BUNDLE",
        help="output bundle file name",
        type=Path,
        required=True,
    )
    fw_bundle_create_parser.add_argument(
        "bootfs",
        metavar="BOARD_FS",
        help="[PREFIX FS..] pairs (e.g. P150A-1 build-p150a/tt_boot_fs.bin)",
        nargs="*",
        default=[],
    )
    # Combine multiple firmware bundles
    fw_bundle_combine_parser = subparsers.add_parser(
        "combine", help="Combine multiple firmware bundles"
    )
    fw_bundle_combine_parser.set_defaults(func=invoke_combine_fw_bundle)
    fw_bundle_combine_parser.add_argument(
        "-o",
        "--output",
        metavar="BUNDLE",
        help="output bundle file name",
        type=Path,
        required=True,
    )
    fw_bundle_combine_parser.add_argument(
        "bundles",
        metavar="BUNDLES",
        help="input bundle files to combine",
        nargs="+",
        default=[],
    )
    # List contents of a firmware bundle
    fw_bundle_ls_parser = subparsers.add_parser(
        "ls", help="List contents of a firmware bundle"
    )
    fw_bundle_ls_parser.set_defaults(func=invoke_ls_fw_bundle)
    fw_bundle_ls_parser.add_argument(
        "-b",
        "--board",
        default="",
        metavar="BOARD",
        help="board prefix to list (e.g. P150A-1)",
    )
    fw_bundle_ls_parser.add_argument(
        "-j",
        "--json",
        help="output in JSON format",
        action="store_true",
    )
    fw_bundle_ls_parser.add_argument(
        "bundle",
        metavar="BUNDLE",
        help="input bundle file to list",
        type=Path,
    )
    # Diff two firmware bundles
    fw_bundle_diff_parser = subparsers.add_parser(
        "diff", help="Diff two firmware bundles"
    )
    fw_bundle_diff_parser.set_defaults(func=invoke_diff_fw_bundles)
    fw_bundle_diff_parser.add_argument(
        "bundle1",
        metavar="BUNDLE1",
        help="first bundle file to compare",
        type=Path,
    )
    fw_bundle_diff_parser.add_argument(
        "bundle2",
        metavar="BUNDLE2",
        help="second bundle file to compare",
        type=Path,
    )
    # Extract a binary from a firmware bundle
    fw_bundle_extract_parser = subparsers.add_parser(
        "extract", help="Extract a binary from a firmware bundle"
    )
    fw_bundle_extract_parser.set_defaults(func=invoke_extract_fw_bundle)
    fw_bundle_extract_parser.add_argument(
        "bundle",
        metavar="BUNDLE",
        help="input bundle file to extract from",
        type=Path,
    )
    fw_bundle_extract_parser.add_argument(
        "-b",
        "--board",
        metavar="BOARD",
        help="board prefix (e.g. P150A-1)",
        required=True,
    )
    fw_bundle_extract_parser.add_argument(
        "-t",
        "--tag",
        metavar="TAG",
        help="image tag to extract (e.g. bootloader)",
        required=True,
    )
    fw_bundle_extract_parser.add_argument(
        "-o",
        "--output",
        metavar="OUTPUT",
        help="output file for extracted binary",
        type=Path,
        required=True,
    )
    # Compare flash dump to fwbundle board image
    fw_bundle_compare_parser = subparsers.add_parser(
        "compare", help="Compare flash dump to fwbundle board image"
    )
    fw_bundle_compare_parser.set_defaults(func=invoke_compare_flash_to_bundle)
    fw_bundle_compare_parser.add_argument(
        "-b",
        "--board",
        metavar="BOARD",
        help="board prefix (e.g. P150A-1, P300C-1_left)",
        required=True,
    )
    fw_bundle_compare_parser.add_argument(
        "flash",
        metavar="FLASH",
        help="local SPI flash dump (.bin or .hex)",
        type=Path,
    )
    fw_bundle_compare_parser.add_argument(
        "bundle",
        metavar="BUNDLE",
        help="firmware bundle to compare against",
        type=Path,
    )
    fw_bundle_compare_parser.add_argument(
        "--report",
        metavar="REPORT",
        help="write markdown comparison report",
        type=Path,
    )
    fw_bundle_compare_parser.add_argument(
        "--csv",
        metavar="CSV",
        help="write per-tag comparison CSV",
        type=Path,
    )
    fw_bundle_compare_parser.add_argument(
        "--diff-offsets-dir",
        metavar="DIR",
        help="write per-tag diff offset lists for non-identical payloads",
        type=Path,
    )
    fw_bundle_compare_parser.add_argument(
        "-j",
        "--json",
        help="output summary in JSON format",
        action="store_true",
    )

    args = parser.parse_args()
    if not hasattr(args, "func"):
        print("No command specified")
        parser.print_help()
        sys.exit(os.EX_USAGE)

    return args


def main():
    """
    Main entry point for tt_fwbundle script.
    """
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
