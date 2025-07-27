#!/bin/bash

# Copyright (c) 2025 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

# Directory containing the tenstorrent devices
DEVICE_DIR="/dev/tenstorrent"
# Command to run
TT_CONSOLE="./tt-console"
# Output directory for logs
LOG_DIR="tt_console_logs"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Check if tt-console executable exists
if [ ! -x "$TT_CONSOLE" ]; then
    echo "Error: $TT_CONSOLE not found or not executable"
    exit 1
fi

# Check if device directory exists
if [ ! -d "$DEVICE_DIR" ]; then
    echo "Error: $DEVICE_DIR directory not found"
    exit 1
fi

# Function to run tt-console for a device
run_tt_console() {
    local device_file="$1"
    local device_name=$(basename "$device_file")
    local log_file="$LOG_DIR/tt_console_${device_name}_$(date +%Y%m%d_%H%M%S).log"

    echo "Running tt-console for device: $device_file"
    echo "Logging to: $log_file"

    # Run tt-console with the device and log output
    "$TT_CONSOLE" -w 500 -d "$device_file" > "$log_file" 2>&1

    local exit_code=$?
    echo "Finished processing $device_file (exit code: $exit_code)"

    return $exit_code
}

# Main execution
if [ $# -eq 0 ]; then
    echo "Starting tt-console runs for all devices in $DEVICE_DIR"
else
    echo "Starting tt-console runs for specified devices: $*"
fi
echo "Logs will be saved to $LOG_DIR"

# Array to store background process PIDs
pids=()
# Counter for processed devices
processed=0

# Check if specific interface IDs were provided
if [ $# -eq 0 ]; then
    # Original behavior: iterate through all files in the tenstorrent device directory
    for device_file in "$DEVICE_DIR"/*; do
        # Check if file exists (handles case where directory is empty)
        if [ ! -e "$device_file" ]; then
            echo "No devices found in $DEVICE_DIR"
            break
        fi

        # Skip if it's not a character device or block device
        if [ ! -c "$device_file" ] && [ ! -b "$device_file" ]; then
            echo "Skipping $device_file (not a device file)"
            continue
        fi

        # Run tt-console for this device in the background
        run_tt_console "$device_file" &
        pids+=($!)  # Store the PID of the background process

        ((processed++))
    done
else
    # New behavior: iterate through the provided interface IDs
    for interface_id in "$@"; do
        device_file="$DEVICE_DIR/$interface_id"

        # Check if device file exists
        if [ ! -e "$device_file" ]; then
            echo "Warning: Device $device_file not found, skipping"
            continue
        fi

        # Skip if it's not a character device or block device
        if [ ! -c "$device_file" ] && [ ! -b "$device_file" ]; then
            echo "Warning: $device_file is not a device file, skipping"
            continue
        fi

        # Run tt-console for this device in the background
        run_tt_console "$device_file" &
        pids+=($!)  # Store the PID of the background process

        ((processed++))
    done
fi

echo "Started $processed parallel tt-console processes"
if [ $processed -gt 0 ]; then
    echo "Process PIDs: ${pids[*]}"
else
    echo "No valid devices found to process"
    exit 1
fi

# Wait for all background processes to complete
failed=0
for pid in "${pids[@]}"; do
    wait $pid
    exit_code=$?
    if [ $exit_code -ne 0 ]; then
        ((failed++))
    fi
done

# Summary
echo "============================================"
echo "Processing complete!"
echo "Total devices processed: $processed"
echo "Failed runs: $failed"
echo "Successful runs: $((processed - failed))"
echo "Logs saved in: $LOG_DIR"
echo "============================================"

# List the generated log files
echo "Generated log files:"
ls -la "$LOG_DIR"/
