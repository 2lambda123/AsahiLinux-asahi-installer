#!/usr/bin/python3
# SPDX-License-Identifier: MIT
import os, os.path, shlex, subprocess, sys, time, termios, json
from dataclasses import dataclass

import system, osenum, stub, diskutil, osinstall, firmware
from util import *

STUB_SIZE = align_up(2500 * 1000 * 1000, 1024 * 1024)

@dataclass
class IPSW:
    version: str
    min_macos: str
    min_iboot: str
    paired_sfr: bool
    url: str

CHIP_MIN_VER = {
    0x8103: "11.0",     # T8103, M1
    0x6000: "12.0",     # T6000, M1 Pro
    0x6001: "12.0",     # T6001, M1 Max
}

DEVICE_MIN_VER = {
    "j274ap": "11.0",   # Mac mini (M1, 2020)
    "j293ap": "11.0",   # MacBook Pro (13-inch, M1, 2020)
    "j313ap": "11.0",   # MacBook Air (M1, 2020)
    "j456ap": "11.3",   # iMac (24-inch, M1, 2021)
    "j457ap": "11.3",   # iMac (24-inch, M1, 2021)
    "j314cap": "12.0",  # MacBook Pro (14-inch, M1 Max, 2021)
    "j314sap": "12.0",  # MacBook Pro (14-inch, M1 Pro, 2021)
    "j316cap": "12.0",  # MacBook Pro (16-inch, M1 Max, 2021)
    "j316sap": "12.0",  # MacBook Pro (16-inch, M1 Pro, 2021)
}

IPSW_VERSIONS = [
    IPSW("12.1",
         "12.1",
         "iBoot-7429.61.2",
         False,
         "https://updates.cdn-apple.com/2021FCSWinter/fullrestores/002-42433/F3F6D5CD-67FE-449C-9212-F7409808B6C4/UniversalMac_12.1_21C52_Restore.ipsw"),
]

class InstallerMain:
    def __init__(self):
        self.data = json.load(open("installer_data.json"))

    def choice(self, prompt, options, default=None):
        is_array = False
        if isinstance(options, list):
            is_array = True
            options = {(i+1): v for i, v in enumerate(options)}
            if default is not None:
                default += 1

        int_keys = all(isinstance(i, int) for i in options.keys())

        for k, v in options.items():
            print(f"  {k}: {v}")

        if default:
            prompt += f" ({default})"

        while True:
            res = input(prompt + ": ")
            if res == "" and default is not None:
                res = default
            if is_array:
                res = int(res)
            if res not in options:
                print(f"Enter one of the following: {', '.join(map(str, options.keys()))}")
                continue
            print()
            if is_array:
                return res - 1
            else:
                return res

    def check_cur_os(self):
        if self.cur_os is None:
            print("Unable to determine primary OS.")
            print("This installer requires you to already have a macOS install with")
            print("at least one administrator user that is a machine owner.")
            print("Please run this installer from your main macOS instance or its")
            print("paired recovery, or ensure the boot device is set correctly.")
            sys.exit(1)

        print(f"Using OS '{self.cur_os.label}' ({self.cur_os.sys_volume}) for machine authentication.")
        logging.info(f"Current OS: {self.cur_os.label} / {self.cur_os.sys_volume}")

    def action_install_into_container(self, avail_parts):
        self.check_cur_os()

        template = self.choose_os()

        containers = {str(i): p.desc for i,p in enumerate(self.parts) if p in avail_parts}

        print()
        print("Choose a container to install into:")
        idx = self.choice("Target container", containers)
        self.part = self.parts[int(idx)]

        ipsw = self.choose_ipsw(template.get("supported_fw", None))
        logging.info(f"Chosen IPSW version: {ipsw.version}")

        self.ins = stub.StubInstaller(self.sysinfo, self.dutil, self.osinfo, ipsw)
        self.osins = osinstall.OSInstaller(self.dutil, self.data, template)
        self.osins.load_package()

        self.do_install()

    def action_install_into_free(self, avail_free):
        self.check_cur_os()

        template = self.choose_os()

        self.osins = osinstall.OSInstaller(self.dutil, self.data, template)
        self.osins.load_package()

        frees = {str(i): p.desc for i,p in enumerate(self.parts) if p in avail_free}

        print()
        print("Choose a free area to install into:")
        idx = self.choice("Target area", frees)
        free_part = self.parts[int(idx)]

        label = input(f"Enter a name for your OS ({self.osins.name}): ") or self.osins.name
        self.osins.name = label
        logging.info(f"New OS name: {label}")
        print()

        ipsw = self.choose_ipsw(template.get("supported_fw", None))
        logging.info(f"Chosen IPSW version: {ipsw.version}")
        self.ins = stub.StubInstaller(self.sysinfo, self.dutil, self.osinfo, ipsw)

        print(f"Creating new stub macOS named {label}")
        logging.info(f"Creating stub macOS: {label}")
        self.part = self.dutil.addPartition(free_part.name, "apfs", label, STUB_SIZE)

        self.do_install()

    def do_install(self):
        print(f"Installing stub macOS into {self.part.name} ({self.part.label})")

        self.ins.prepare_volume(self.part)
        self.ins.check_volume()
        self.ins.install_files(self.cur_os)

        self.osins.partition_disk(self.part.name)

        pkg = None
        if self.osins.needs_firmware:
            pkg = firmware.FWPackage("firmware.tar")
            self.ins.collect_firmware(pkg)
            pkg.close()
            self.osins.firmware_package = pkg

        self.osins.install(self.ins.boot_obj_path)

        for i in self.osins.idata_targets:
            self.ins.collect_installer_data(i)
            shutil.copy("installer.log", os.path.join(i, "installer.log"))

        self.step2()

    def choose_ipsw(self, supported_fw=None):
        sys_iboot = split_ver(self.sysinfo.sys_firmware)
        sys_macos = split_ver(self.sysinfo.macos_ver)
        chip_min = split_ver(CHIP_MIN_VER.get(self.sysinfo.chip_id, "0"))
        device_min = split_ver(DEVICE_MIN_VER.get(self.sysinfo.device_class, "0"))
        minver = [ipsw for ipsw in IPSW_VERSIONS
                 if split_ver(ipsw.version) >= max(chip_min, device_min)
                 and (supported_fw is None or ipsw.version in supported_fw)]
        avail = [ipsw for ipsw in minver
                 if split_ver(ipsw.min_iboot) <= sys_iboot
                 and split_ver(ipsw.min_macos) <= sys_macos]

        if not avail:
            print("Your system firmware is too old.")
            print(f"Please upgrade to macOS {minver[0].version} or newer.")
            sys.exit(1)

        if len(avail) > 1:
            print("Choose the macOS version to use for boot firmware:")
            print("(If unsure, just press enter)")
            idx = self.choice("Version", [i.version for i in avail], len(avail)-1)
        else:
            idx = 0

        self.ipsw = ipsw = avail[idx]
        print(f"Using macOS {ipsw.version} for OS firmware")
        print()

        return ipsw

    def choose_os(self):
        print("Choose an OS to install:")
        idx = self.choice("OS", [i["name"] for i in self.data["os_list"]])
        os = self.data["os_list"][idx]
        logging.info(f"Chosen OS: {os['name']}")
        return os

    def set_reduced_security(self):
        print()
        print( "We are about to prepare your new stub OS for booting in")
        print( "Reduced Security mode. Please enter your macOS credentials")
        print( "when prompted.")
        print()
        print( "Press enter to continue.")
        input()

        while True:
            try:
                subprocess.run(["bputil", "-g", "-v", self.ins.osi.vgid], check=True)
                break
            except subprocess.CalledProcessError:
                print("Failed to run bputil. Press enter to try again.")
                input()

        print()

    def step2(self):
        is_1tr = self.sysinfo.boot_mode == "one true recoveryOS"
        is_recovery = "recoveryOS" in self.sysinfo.boot_mode
        bootpicker_works = split_ver(self.sysinfo.macos_ver) >= split_ver(self.ipsw.min_macos)

        if is_1tr and self.is_sfr_recovery and self.ipsw.paired_sfr:
            subprocess.run([self.ins.step2_sh], check=True)
            self.startup_disk(recovery=True, volume_blessed=True, reboot=True)
        elif is_recovery:
            self.set_reduced_security()
            self.startup_disk(recovery=True, volume_blessed=True)
            self.step2_indirect()
        elif bootpicker_works:
            self.startup_disk()
            self.step2_indirect()
        else:
            assert False # should never happen, we don't give users the option

    def step2_1tr_direct(self):
        self.startup_disk_recovery()
        subprocess.run([self.ins.step2_sh], check=True)

    def step2_ros_indirect(self):
        self.startup_disk_recovery()

    def flush_input(self):
        try:
            termios.tcflush(sys.stdin, termios.TCIOFLUSH)
        except:
            pass

    def step2_indirect(self):
        # Hide the new volume until step2 is done
        os.rename(self.ins.systemversion_path,
                  self.ins.systemversion_path.replace("SystemVersion.plist",
                                                      "SystemVersion-disabled.plist"))

        print( "The system will now shut down.")
        print( "To complete the installation, perform the following steps:")
        print()
        print( "1. Wait 10 seconds for the system to fully shut down.")
        print( "2. Press and hold down the power button to power on the system.")
        print( "   * It is important that the system be fully powered off before this step,")
        print( "     and that you press and hold down the button once, not multiple times.")
        print( "     This is required to put the machine into the right mode.")
        print( "3. Release it once 'Entering startup options' is displayed.")
        print(f"4. Choose {self.part.label}.")
        print( "5. You will briefly see a 'macOS Recovery' dialog.")
        print( "   * If you are asked to 'Select a volume to recover',")
        print( "     then choose your normal macOS volume and click Next.")
        print( "6. Once the 'Asahi Linux installer' screen appears, follow the prompts.")
        print()
        time.sleep(2)
        self.flush_input()
        print( "Press enter to shut down the system.")
        input()
        os.system("shutdown -h now")

    def startup_disk(self, recovery=False, volume_blessed=False, reboot=False):
        print()
        print(f"When the Startup Disk screen appears, choose '{self.part.label}', then click Restart.")
        if not volume_blessed:
            print( "You will have to authenticate yourself.")
        print()
        print( "Press enter to continue.")
        input()

        if recovery:
            args = ["/System/Applications/Utilities/Startup Disk.app/Contents/MacOS/Startup Disk"]
        else:
            os.system("killall -9 'System Preferences' 2>/dev/null")
            os.system("killall -9 storagekitd 2>/dev/null")
            time.sleep(0.5)
            args = ["sudo", "-u", self.sysinfo.login_user,
                    "open", "-b", "com.apple.systempreferences",
                    "/System/Library/PreferencePanes/StartupDisk.prefPane"]

        sd = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not recovery:
            # Sometimes this doesn't open the right PrefPane and we need to do it twice (?!)
            sd.wait()
            time.sleep(0.5)
            sd = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cur_vol = self.sysinfo.default_boot

        # This race is tight... I hate this.
        if not reboot:
            while self.sysinfo.default_boot == cur_vol:
                self.sysinfo.get_nvram_data()

            if recovery:
                sd.kill()
            else:
                os.system("killall -9 StartupDiskPrefPaneService 'System Preferences' 2>/dev/null")
                sd.wait()

            print()

    def main(self):
        print()
        print("Welcome to the Asahi Linux installer!")
        if "-v" in sys.argv:
            print()
            print("* Verbose mode enabled.")
        print()
        print("This installer is in a pre-alpha state, and will only do basic")
        print("bootloader set-up for you. It is only intended for developers")
        print("who wish to help with Linux bring-up at this point.")
        print()
        print("Please make sure you are familiar with our documentation at:")
        print("  https://alx.sh/w")
        print()
        print("Press enter to continue.")
        input()
        print()

        print("Collecting system information...")
        self.sysinfo = system.SystemInfo()
        self.sysinfo.show()
        print()

        if self.sysinfo.boot_mode == "macOS" and (
            (not self.sysinfo.login_user)
            or self.sysinfo.login_user == "unknown"):
            print("Could not detect logged in user.")
            print("Perhaps you are running this installer over SSH?")
            print("Please make sure a user is logged into the local console.")
            print("You can use SSH as long as there is a local login session.")
            sys.exit(1)

        print("Collecting partition information...")
        self.dutil = diskutil.DiskUtil()
        self.dutil.get_info()
        self.sysdsk = self.dutil.find_system_disk()
        print(f"  System disk: {self.sysdsk}")
        self.parts = self.dutil.get_partitions(self.sysdsk)
        print()

        print("Collecting OS information...")
        self.osinfo = osenum.OSEnum(self.sysinfo, self.dutil, self.sysdsk)
        self.osinfo.collect(self.parts)

        parts_free = []
        parts_empty_apfs = []
        parts_system = []

        for i, p in enumerate(self.parts):
            if p.type in ("Apple_APFS_ISC",):
                continue
            if p.free:
                p.desc = f"(free space: {ssize(p.size)})"
                if p.size >= STUB_SIZE:
                    parts_free.append(p)
            elif p.type.startswith("Apple_APFS"):
                p.desc = "APFS"
                if p.type == "Apple_APFS_Recovery":
                    p.desc += " (System Recovery)"
                if p.label is not None:
                    p.desc += f" [{p.label}]"
                vols = p.container["Volumes"]
                p.desc += f" ({ssize(p.size)}, {len(vols)} volume{'s' if len(vols) != 1 else ''})"
                if p.os and any(os.version for os in p.os):
                    parts_system.append(p)
                else:
                    if p.size >= STUB_SIZE * 0.95:
                        parts_empty_apfs.append(p)
            else:
                p.desc = f"{p.type} ({ssize(p.size)})"

        print()
        print(f"Partitions in system disk ({self.sysdsk}):")

        self.cur_os = None
        self.is_sfr_recovery = self.sysinfo.boot_vgid in (osenum.UUID_SROS, osenum.UUID_FROS)
        default_os = None

        for i, p in enumerate(self.parts):
            if p.desc is None:
                continue
            print(f"  {i}: {p.desc}")
            if not p.os:
                continue
            for os in p.os:
                if not os.version:
                    continue
                state = " "
                if self.sysinfo.boot_vgid == os.vgid and self.sysinfo.boot_uuid == os.rec_vgid:
                    if p.type == "APFS":
                        self.cur_os = os
                    state = "R"
                elif self.sysinfo.boot_uuid == os.vgid:
                    self.cur_os = os
                    state = "B"
                elif self.sysinfo.boot_vgid == os.vgid:
                    state = "?"
                if self.sysinfo.default_boot == os.vgid:
                    default_os = os
                    state += "*"
                else:
                    state += " "
                print(f"    OS: [{state}] {os}")

        if self.cur_os is None:
            self.cur_os = default_os

        print()
        print("  [B ] = Booted OS, [R ] = Booted recovery, [? ] = Unknown")
        print("  [ *] = Default boot volume")
        print()
        actions = {}

        if parts_free:
            actions["f"] = "Install an OS into free space"
        if parts_empty_apfs:
            actions["a"] = "Install an OS into an existing APFS container"
        if parts_system and False:
            actions["r"] = "Resize an existing OS and install a new OS"
            if self.sysinfo.boot_mode == "one true recoveryOS":
                actions["m"] = "Upgrade bootloader of an existing OS"

        if not actions:
            print("No actions available on this system.")
            sys.exit(1)

        actions["q"] = "Quit without doing anything"

        print("Choose what to do:")
        act = self.choice("Action", actions, "q")

        if act == "f":
            self.action_install_into_free(parts_free)
        elif act == "a":
            self.action_install_into_container(parts_empty_apfs)
        elif act == "r":
            print("Unimplemented")
            sys.exit(1)
        elif act == "m":
            print("Unimplemented")
            sys.exit(1)
        elif act == "q":
            sys.exit(0)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                        datefmt='%m-%d %H:%M',
                        filename='installer.log',
                        filemode='w')

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    formatter = logging.Formatter('%(name)-12s: %(levelname)-8s %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)
    logging.info("Startup")

    logging.info("Environment:")
    for var in ("INSTALLER_BASE", "INSTALLER_DATA", "REPO_BASE", "IPSW_BASE"):
        logging.info(f"  {var}={os.environ.get(var, None)}")

    InstallerMain().main()
