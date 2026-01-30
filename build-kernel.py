import os
import sys
import subprocess
import shutil
import argparse
import re
from datetime import datetime
import utils

# Load config variables
DEFCONFIG = os.environ.get("CONFIG_DEFCONFIG")
AK3_REPO = os.environ.get("CONFIG_AK3_REPO")
KSU_URL = os.environ.get("CONFIG_KSU_URL", "https://raw.githubusercontent.com/tiann/KernelSU/main/kernel/setup.sh")
CUSTOM_COMMANDS = os.environ.get("CONFIG_KERNEL_CUSTOM_COMMANDS")

if not all([utils.config.BOT_TOKEN, utils.config.CHAT_ID, DEFCONFIG]):
    print("ERROR: Missing configuration (BOT_TOKEN, CHATID, or DEFCONFIG).")
    sys.exit(1)

JOBS_FLAG = utils.get_jobs_flag()
DISPLAY_JOBS = JOBS_FLAG.replace("-j", "")

KERNEL_OUT = "out/arch/arm64/boot"
ANYKERNEL_DIR = "AnyKernel3"
LOG_FILE = "build.log"

def get_git_head():
    try:
        short_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
        full_hash = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        origin = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], text=True
        ).strip()

        if origin.endswith(".git"):
            origin = origin[:-4]

        return f"<a href='{origin}/commit/{full_hash}'>{short_hash}</a>"
    except Exception:
        return "Unknown"


def get_localversion():
    config_path = "out/.config"
    if not os.path.exists(config_path):
        return "N/A"
    try:
        with open(config_path, "r") as f:
            for file_line in f:
                if file_line.strip().startswith("CONFIG_LOCALVERSION="):
                    return file_line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return "N/A"


def get_compiler_version():
    try:
        output = subprocess.check_output(["clang", "--version"], text=True).strip()
        first_line = output.splitlines()[0] if output else ""
        match = re.search(r"clang version \d+\.\d+\.\d+", first_line)
        if match:
            return match.group(0)
        return "Clang/LLVM"
    except Exception as e:
        print(f"Compiler check error: {e}")
        return "Clang/LLVM"


def get_compiled_version_string():
    image_path = os.path.join(KERNEL_OUT, "Image")
    if not os.path.exists(image_path):
        return None
    try:
        cmd = f"strings {image_path} | grep 'Linux version [0-9]' | head -n 1"
        line_out = subprocess.check_output(cmd, shell=True, text=True).strip()
        match = re.search(r"Linux version (.*)", line_out)
        if match:
            return match.group(1)
    except Exception as e:
        print(f"Version Extraction Error: {e}")
        pass
    return None


# Package the kernel using AnyKernel3
def package_anykernel(version_string, ksu_enabled=False):
    print("Packaging AnyKernel3...")

    if os.path.exists(ANYKERNEL_DIR):
        print("AnyKernel3 detected. Updating repository...")
        subprocess.call(["git", "-C", ANYKERNEL_DIR, "pull", "-q"])

        import glob

        for old_zip in glob.glob(os.path.join(ANYKERNEL_DIR, "*.zip")):
            os.remove(old_zip)
    else:
        print("Cloning AnyKernel3...")
        subprocess.call(["git", "clone", "-q", AK3_REPO, ANYKERNEL_DIR])

    # Default map
    files_map = {"Image.gz": "Image.gz", "dtbo.img": "dtbo.img", "dtb.img": "dtb"}

    # Handle custom files map from config
    user_map_env = os.environ.get("CONFIG_FILES_MAP")

    if user_map_env:
        try:
            custom_map = {}
            for pair in user_map_env.split(";"):
                if ":" in pair:
                    src, dst = pair.split(":", 1)
                    custom_map[src.strip()] = dst.strip()

            if custom_map:
                files_map = custom_map
                print(f"Custom files map loaded: {files_map}")
            else:
                print("Warning: CONFIG_FILES_MAP is empty or invalid. Using default.")
        except Exception as e:
            print(f"Error reading CONFIG_FILES_MAP: {e}. Using default.")

    # Copy files to AnyKernel
    for src_name, dst_name in files_map.items():
        src = os.path.join(KERNEL_OUT, src_name)
        dst = os.path.join(ANYKERNEL_DIR, dst_name)
        if os.path.exists(src):
            shutil.copy(src, dst)
            print(f"Copied: {src_name} -> {dst_name}")
        else:
            print(f"Warning: Source file not found: {src}")

    zip_name = None
    if version_string:
        match = re.search(r"^(\S+).*?(\w{3})\s+(\w{3})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2}).*$", version_string)
        if match:
            kernel_version = match.group(1).replace("-dirty", "")
            day_week = match.group(2)
            month = match.group(3)
            day_num = match.group(4)
            time_str = match.group(5).replace(":", "")
            zip_name = f"{kernel_version}-{day_week}{month}{day_num}-{time_str}.zip"

    if not zip_name:
        timestamp = datetime.now().strftime("%Y%a%b%d-%H%M%S")
        ver_tag = version_string.split()[0] if version_string else "Unknown-Kernel"
        zip_name = f"{ver_tag}-{timestamp}.zip"

    if ksu_enabled:
        zip_name = f"KSU-{zip_name}"

    cwd = os.getcwd()
    os.chdir(ANYKERNEL_DIR)

    # Create the zip package
    zip_cmd = [
        "zip",
        "-r9",
        "-q",
        f"../{zip_name}",
        ".",
        "-x",
        ".git*",
        "README.md",
        "*placeholder",
        ".gitignore",
    ]
    subprocess.call(zip_cmd)
    os.chdir(cwd)

    if os.path.exists(zip_name):
        return os.path.abspath(zip_name)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--clean", action="store_true")
    parser.add_argument("--ksu", action="store_true", help="Enable KernelSU support")
    args = parser.parse_args()

    if args.ksu:
        print("Setting up KernelSU...")
        ksu_setup_cmd = f'curl -LSs "{KSU_URL}" | bash'
        try:
            subprocess.run(ksu_setup_cmd, shell=True, check=True)
            print("KernelSU setup successful.")
        except subprocess.CalledProcessError as e:
            print(f"Error setting up KernelSU: {e}", file=sys.stderr)
            sys.exit(1)

    # Clean output if requested
    if args.clean and os.path.exists("out"):
        print("Cleaning out/...")
        shutil.rmtree("out")

    git_head_link = get_git_head()
    compiler_ver = get_compiler_version()

    base_info = (
        f"<b>Head:</b> <code>{git_head_link}</code>\n"
        f"{utils.line('Defconfig', DEFCONFIG)}\n"
        f"{utils.line('Jobs', DISPLAY_JOBS)}\n"
        f"{utils.line('Compiler', compiler_ver)}"
    )

    # Configure the output
    cmd_config = ["make", "O=out", "ARCH=arm64", "LLVM=1"] + DEFCONFIG.split()
    print(f"Configuring: {DEFCONFIG}")
    subprocess.call(cmd_config)

    # Execute custom commands if provided
    if CUSTOM_COMMANDS:
        subprocess.run(CUSTOM_COMMANDS, shell=True, executable="/bin/bash")

    local_ver = get_localversion()

    # Update info with local version
    base_info = (
        f"<b>Head:</b> <code>{git_head_link}</code>\n"
        f"{utils.line('Local Version', local_ver)}\n"
        f"{utils.line('Defconfig', DEFCONFIG)}\n"
        f"{utils.line('Jobs', DISPLAY_JOBS)}\n"
        f"{utils.line('Compiler', compiler_ver)}"
    )

    # Build command
    build_cmd = [
        "make",
        JOBS_FLAG,
        "O=out",
        "ARCH=arm64",
        "LLVM=1",
        "Image.gz",
        "dtbo.img",
        "dtb.img",
    ]
    print(f"Building: {' '.join(build_cmd)}")

    runner = utils.BuildRunner(build_cmd, base_info, LOG_FILE)
    final_build_msg, msg_id = runner.run()

    # Package
    compiled_ver_str = get_compiled_version_string()
    final_zip = package_anykernel(compiled_ver_str, args.ksu)

    if not final_zip:
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["upload_fail"].format(
                build_msg=final_build_msg, reason="Could not create ZIP."
            ),
        )
        sys.exit(1)

    # Upload files
    files_to_upload = [("Download", final_zip)]
    utils.upload_artifacts(final_build_msg, msg_id, files_to_upload, final_zip)

if __name__ == "__main__":
    main()
