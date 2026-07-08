.. _tt_smc_boot_sequence:

SMC boot sequence
*****************

The SMC (System Management Controller) firmware is the Blackhole **CMFW**: a Zephyr application
whose sources live under ``app/smc`` and whose hardware bring-up logic lives primarily in
``lib/tenstorrent/bh_arc``. How the ASIC ROM and tt-boot-fs load MCUBoot and the CMFW image is
documented in :ref:`tt_z_p_bootloader`. This page describes the **runtime sequence inside the CMFW
image** after MCUBoot has jumped into Zephyr: Zephyr init hooks, the ordered ``POST_KERNEL`` chain
controlled by ``include/tenstorrent/sys_init_defines.h``, and ``main()`` in ``app/smc/src/main.c``.

High-level flow
===============

.. graphviz::
   :caption: CMFW runtime path after reset (see :ref:`tt_z_p_bootloader` for ROM and MCUBoot detail)

   digraph smc_boot_overview {
       rankdir=LR
       node [shape=box];

       ROM -> BootFS -> MCUBoot -> ZephyrKernel -> EarlyInit -> PostKernel -> main -> WatchdogLoop;

       ROM [label="ROM bootloader"];
       BootFS [label="tt_boot_fs"];
       MCUBoot [label="MCUBoot"];
       ZephyrKernel [label="Zephyr kernel"];
       EarlyInit [label="EARLY SYS_INIT"];
       PostKernel [label="POST_KERNEL chain"];
       main [label="main()"];
       WatchdogLoop [label="Watchdog loop"];
   }

Zephyr runs **EARLY**-phase ``SYS_INIT`` hooks first, then **POST_KERNEL** hooks in ascending
priority order (lower numbers run first). The Tenstorrent boot banner uses ``POST_KERNEL`` priority
``0``, so it runs before any ``SYS_INIT_APP`` step (priorities 90–113).

``SYS_INIT_APP`` macro
======================

``SYS_INIT_APP(func)`` is defined in ``include/tenstorrent/sys_init_defines.h`` as
``SYS_INIT(func, POST_KERNEL, func##_PRIO)``. Each init function therefore picks up a priority
from a matching ``func_PRIO`` macro in the same header (for example ``PLLInit`` uses
``PLLInit_PRIO``). **Smaller priority values run earlier.** The ``*_PRIO`` symbols below are the
authoritative ordering for Blackhole ARC application init.

Init priorities (``sys_init_defines.h``)
==========================================

The following paragraphs correspond to each ``*_PRIO`` symbol in
``include/tenstorrent/sys_init_defines.h`` (90 through 113).

``register_interrupt_handlers_PRIO`` (90)
-------------------------------------------

Registered in ``lib/tenstorrent/bh_arc/msgqueue.c`` as ``SYS_INIT_APP(register_interrupt_handlers)``
(unless ``MSG_QUEUE_TEST`` is defined). The function walks the ``msgqueue_handler`` linker section
and installs handlers for each message type. This runs early in ``POST_KERNEL`` so subsequent init
and runtime can rely on the message-queue dispatch path.

``arc_dma_init_PRIO`` (91)
--------------------------

No ``SYS_INIT_APP(arc_dma_init)`` (or other use of this priority) appears in this repository. The
priority is reserved in the header for a future or out-of-tree ARC DMA init step; it currently
behaves as a **gap** between priority 90 and 92—nothing runs at priority 91.

``InitSpiFS_PRIO`` (92)
-----------------------

Registered in ``lib/tenstorrent/bh_arc/spi_eeprom.c`` as ``SYS_INIT_APP(InitSpiFS)``. On ARC builds
it calls ``EepromSetup()`` to initialize access to SPI EEPROM used for firmware tables and related
data. Non-ARC builds skip work.

``bh_arc_init_start_PRIO`` (93)
-------------------------------

Registered in ``app/smc/src/main.c`` as ``SYS_INIT_APP(bh_arc_init_start)``. Sets
``hw_init_status`` in the boot status register to indicate hardware init has started and emits ARC
post codes for early bring-up visibility.

``CATEarlyInit_PRIO`` (94)
--------------------------

Registered in ``lib/tenstorrent/bh_arc/cat.c`` as ``SYS_INIT_APP(CATEarlyInit)``. Enables the CAT
thermal trip path early with a conservative trim so the chip is protected before full calibration.

``CalculateHarvesting_PRIO`` (95)
---------------------------------

Registered in ``lib/tenstorrent/bh_arc/harvesting.c`` as
``SYS_INIT_APP(CalculateHarvesting)``. Derives which Tensix columns, GDDR instances, Ethernet
tiles, and PCIe usage are enabled from the firmware table, including cable-fault and harvesting
rules. Later inits depend on the global ``tile_enable`` state this fills in.

``DeassertTileResets_PRIO`` (96)
--------------------------------

Registered in ``lib/tenstorrent/bh_arc/reset.c`` as ``SYS_INIT_APP(DeassertTileResets)``. After
returning PLLs to bypass for safe reset sequencing, deasserts global NOC, system, PCIe, PTP, ETH,
Tensix, DDR, and L2CPU reset lines. Skipped for recovery builds or non-ARC; in cable-fault mode still
deasserts resets needed for ARC–PCIe communication while other tiles stay clock-gated later.

``PLLInit_PRIO`` (97)
---------------------

Registered in ``lib/tenstorrent/bh_arc/pll.c`` as ``SYS_INIT_APP(PLLInit)`` (public symbol
``PLLInit``). Reconfigures chip PLLs from reset state, locks them, applies post-dividers, and
enables clock counters—establishing core clocks (e.g. AICLK, AXI/ARC/APB) for the rest of init.
Non-ARC builds skip.

``PVTInit_PRIO`` (98)
---------------------

No ``SYS_INIT_APP(PVTInit)`` exists in this repository; the name is defined only as a priority.
**Priority 98 is currently unused** as a placeholder between ``PLLInit`` and ``NocInit``.

``NocInit_PRIO`` (99)
---------------------

Registered in ``lib/tenstorrent/bh_arc/noc_init.c`` as ``SYS_INIT_APP(NocInit)``. Programs the NOC
mesh: NIU and router configuration, optional clock gating, per-tile clock-off for harvested or
cable-fault tiles, overlay clock gating, and broadcast exclusion for bad Tensix columns. Skipped
for recovery or non-ARC.

``AssertSoftResets_PRIO`` (100)
-------------------------------

Registered in ``lib/tenstorrent/bh_arc/reset.c`` as ``SYS_INIT_APP(AssertSoftResets)``. Asserts soft
reset on Tensix, Ethernet, and GDDR RISC cores (L2CPU intentionally skipped per known issues). Skipped
for recovery, non-ARC, or cable-fault mode.

``DeassertRiscvResets_PRIO`` (101)
----------------------------------

Registered in ``lib/tenstorrent/bh_arc/reset.c`` as ``SYS_INIT_APP(DeassertRiscvResets)``. Temporarily
returns PLLs to bypass, then deasserts RISC-V soft resets for the same tile classes. Skipped for
recovery, non-ARC, or cable-fault mode.

``InitAiclkPPM_PRIO`` (102)
---------------------------

Registered in ``lib/tenstorrent/bh_arc/aiclk_ppm.c`` as ``SYS_INIT_APP(InitAiclkPPM)``. Initializes
AICLK PPM limits and arbiters from the firmware table (min/max frequency, sweep off, host FMAX
arbitration disabled until a host message enables it).

``pcie_init_PRIO`` (103)
------------------------

Registered in ``lib/tenstorrent/bh_arc/pcie.c`` as ``SYS_INIT_APP(pcie_init)``. For each PCIe
controller enabled in the firmware table, runs controller initialization and reset-interrupt setup,
then records a PCIe init completion timestamp in a status register.

``tensix_init_PRIO`` (104)
--------------------------

Registered in ``lib/tenstorrent/bh_arc/tensix_init.c`` as ``SYS_INIT_APP(tensix_init)``. Runs
Tensix bring-up (optional clock gating, UNPACR workaround), L1 wipe, and destination register wipe.
Skipped for recovery, non-ARC, or cable-fault mode.

``InitMrisc_PRIO`` (105)
------------------------

Registered in ``lib/tenstorrent/bh_arc/gddr.c`` as ``SYS_INIT_APP(InitMrisc)``. Sets GDDR memory
clock from configuration, loads MRISC firmware images from SPI for enabled DRAM instances, and
releases MRISC reset so training can proceed.

``eth_init_PRIO`` (106)
-----------------------

Registered in ``lib/tenstorrent/bh_arc/eth.c`` as ``SYS_INIT_APP(eth_init)``. Initializes SerDes and
Ethernet subsystem firmware for enabled ETH tiles. Skipped for recovery, non-ARC, or cable-fault
mode.

``InitSmbusTarget_PRIO`` (107)
------------------------------

Registered in ``lib/tenstorrent/bh_arc/smbus_target.c`` as ``SYS_INIT_APP(InitSmbusTarget)``.
Registers SMBus target commands on the CM–DM I2C interface (telemetry, power, fan, tests, etc.);
recovery builds omit a subset of telemetry-related commands.

``regulator_init_PRIO`` (108)
-----------------------------

Registered in ``lib/tenstorrent/bh_arc/regulator.c`` as ``SYS_INIT_APP(regulator_init)``. Calls
``RegulatorInit()`` for the PCB type from the firmware table; on failure sets
``error_status0.regulator_init_error`` (global in ``reset.c``). Skipped for recovery or non-ARC.

``avs_init_PRIO`` (109)
-----------------------

Registered in ``lib/tenstorrent/bh_arc/avs.c`` as ``SYS_INIT_APP(avs_init)``. Brings up the AVS
interface and switches VOUT control to the AVS path. Skipped for recovery or non-ARC.

``InitNocTranslationFromHarvesting_PRIO`` (110)
-----------------------------------------------

Registered in ``lib/tenstorrent/bh_arc/noc_init.c`` as
``SYS_INIT_APP(InitNocTranslationFromHarvesting)``. If NOC translation is enabled in the firmware
table, computes translation tables from harvesting, PCIe endpoint choice, and ETH/GDDR skip masks,
then programs NOC coordinate translation. Skipped when the feature is disabled or for
recovery/non-ARC.

``gddr_training_PRIO`` (111)
----------------------------

Registered in ``lib/tenstorrent/bh_arc/gddr.c`` as ``SYS_INIT_APP(gddr_training)``. Waits for MRISC
training completion per enabled GDDR instance and runs optional HW memory tests when training
succeeds. Skipped for recovery, non-ARC, or cable-fault mode.

``CATInit_PRIO`` (112)
----------------------

Registered in ``lib/tenstorrent/bh_arc/cat.c`` as ``SYS_INIT_APP(CATInit)`` only when
``CONFIG_TT_SMC_RECOVERY`` is **disabled**. Performs CAT calibration against PVT sensors and
re-enables CAT at the shutdown threshold with offset. Recovery builds skip this entire module path.

``bh_arc_init_end_PRIO`` (113)
------------------------------

Registered in ``app/smc/src/main.c`` as ``SYS_INIT_APP(bh_arc_init_end)``. Last step in the app init
chain: sets ``fw_id`` to normal or recovery based on ``CONFIG_TT_SMC_RECOVERY``, sets
``hw_init_status`` from the global ``tt_init_status``, and writes the aggregated
``error_status0`` value to the error status register.

Application ``main()`` (after all ``SYS_INIT``)
===============================================

After every ``SYS_INIT`` hook has returned, Zephyr calls ``main()`` in ``app/smc/src/main.c``:

#. Sets post code ``POST_CODE_ZEPHYR_INIT_DONE`` and prints the CMFW version string.
#. If not a recovery build and AICLK PPM is enabled in the firmware table, may call ``InitDVFS()``
   unless a regulator init error was recorded.
#. Calls ``init_msgqueue()`` to finish message-queue setup for runtime.
#. If not recovery: starts telemetry, optional fan control from the firmware table, and periodic
   telemetry/DVFS timers (started after inits to reduce I2C contention during boot).
#. Calls ``Dm2CmReadyRequest()`` to signal readiness toward the DM firmware.
#. When ``CONFIG_BOOTLOADER_MCUBOOT`` is set, confirms the running image with MCUBoot so a successful
   boot is not reverted on the next reset.
#. Enters an infinite loop: trace hook, sleep for ``CONFIG_TT_BH_ARC_WDT_FEED_INTERVAL``, feed the
   watchdog.

EARLY hooks in ``app/smc/src/main.c`` (before ``POST_KERNEL``) write the firmware version and
CMFW start timestamp to status registers via ``SYS_INIT`` at priority ``0``.

Recovery image (``CONFIG_TT_SMC_RECOVERY``)
=========================================

The recovery CMFW build sets ``CONFIG_TT_SMC_RECOVERY``. Many ``SYS_INIT_APP`` functions return
immediately without touching hardware. ``main()`` omits DVFS, telemetry, fan control, and the
associated timers. ``bh_arc_init_end`` still runs and tags ``fw_id`` as the recovery image in boot
status.
