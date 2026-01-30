import os
import sys
import time
import subprocess
import re
import glob
import argparse
import utils

# Load config variables
DEVICE = os.environ.get("CONFIG_DEVICE")
TARGET = os.environ.get("CONFIG_BUILD_TARGET")
BUILD_VARIANT = os.environ.get("CONFIG_BUILD_TYPE")
CUSTOM_COMMANDS = os.environ.get("CONFIG_ROM_CUSTOM_COMMANDS")
REC_IMAGES = os.environ.get("CONFIG_RECOVERY_IMAGES")

if not all([utils.config.BOT_TOKEN, utils.config.CHAT_ID, DEVICE, TARGET, BUILD_VARIANT]):
    print("ERROR: Missing configuration (BOT_TOKEN, CHATID, DEVICE, TARGET, or TYPE).")
    sys.exit(1)

JOBS_FLAG = utils.get_jobs_flag()
SYNC_JOBS = utils.get_sync_jobs()

current_folder = os.getcwd().split("/")[-1]
ROM_NAME = os.environ.get("CONFIG_ROM_NAME") or current_folder or "Unknown ROM"


def get_build_vars():
    print("Fetching build system variables...")
    try:
        cmd = (
            f"source build/envsetup.sh && "
            f"breakfast {DEVICE} {BUILD_VARIANT} >/dev/null 2>&1 && "
            f'echo "VER=$(get_build_var PLATFORM_VERSION)" && '
            f'echo "BID=$(get_build_var BUILD_ID)" && '
            f'echo "TYPE=$(get_build_var TARGET_BUILD_VARIANT)"'
        )
        output = subprocess.check_output(
            cmd, shell=True, executable="/bin/bash", text=True
        )
        d = {}
        for out_line in output.splitlines():
            if "=" in out_line:
                k, v = out_line.split("=", 1)
                d[k] = v.strip()
        return d
    except Exception as e:
        print(f"Warning: Could not fetch vars: {e}")
        return {"VER": "N/A", "BID": "N/A", "TYPE": BUILD_VARIANT}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--sync", action="store_true")
    parser.add_argument("-c", "--clean", action="store_true")
    args = parser.args

    # Sync sources if requested
    if args.sync:
        start = time.time()
        details = f"{utils.line('Rom', ROM_NAME)}\n{utils.line('Jobs', SYNC_JOBS)}"
        msg_id = utils.send_msg(utils.MESSAGES["sync_start"].format(details=details))

        cmd = f"repo sync -c -j{SYNC_JOBS} --optimized-fetch --prune --force-sync --no-clone-bundle --no-tags"
        if subprocess.call(cmd.split()) != 0:
            subprocess.call(f"repo sync -j{SYNC_JOBS}".split())

        dur = utils.fmt_time(time.time() - start)
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["sync_done"].format(
                details=utils.line("Rom", ROM_NAME), dur=dur
            ),
        )

    # Clean output
    if args.clean and os.path.exists("out"):
        import shutil

        shutil.rmtree("out")

    # Build Setup
    build_vars = get_build_vars()
    ANDROID_VERSION = build_vars.get("VER", "N/A")
    BUILD_ID = build_vars.get("BID", "N/A")
    REAL_VARIANT = build_vars.get("TYPE", BUILD_VARIANT)

    base_info = (
        f"<b>Rom:</b> <code>{ROM_NAME}</code>\n"
        f"<b>Device:</b> <code>{DEVICE}</code>\n"
        f"<b>Android:</b> <code>{ANDROID_VERSION}</code>\n"
        f"<b>Build ID:</b> <code>{BUILD_ID}</code>\n"
        f"<b>Type:</b> <code>{REAL_VARIANT}</code>"
    )

    build_cmd = f"source build/envsetup.sh && breakfast {DEVICE} {BUILD_VARIANT}"
    if CUSTOM_COMMANDS:
        build_cmd += f" && {CUSTOM_COMMANDS}"
    build_cmd += f" && m {TARGET} {JOBS_FLAG}"
    print(f"Cmd: {build_cmd}")

    runner = utils.BuildRunner(build_cmd, base_info, is_rom=True)
    final_build_msg, msg_id = runner.run()

    # Locate/Prepare artifacts
    out_dir = f"out/target/product/{DEVICE}"
    final_zip = None
    zips = glob.glob(f"{out_dir}/*{DEVICE}*.zip")
    if zips:
        final_zip = max(zips, key=os.path.getctime)

    if not final_zip:
        utils.edit_msg(
            msg_id,
            utils.MESSAGES["upload_fail"].format(
                build_msg=final_build_msg, reason="No ZIP found."
            ),
        )
        sys.exit(1)

    # Packaging recovery images if requested
    rec_zip_path = None
    if REC_IMAGES:
        print("Packaging recovery images...")
        rec_list = re.split(r"[;\s]+", REC_IMAGES)
        cmd_files = []
        for img in rec_list:
            if not img:
                continue
            f_path = os.path.join(out_dir, img)
            if os.path.exists(f_path):
                cmd_files.append(f_path)

        if cmd_files:
            rec_name = f"RECOVERY-{os.path.basename(final_zip)}"
            subprocess.call(["zip", "-j", rec_name] + cmd_files)
            if os.path.exists(rec_name):
                rec_zip_path = rec_name

    # Upload files
    files_to_upload = [("Download", final_zip)]
    if rec_zip_path:
        files_to_upload.append(("Recovery", rec_zip_path))

    json_f = glob.glob(f"{out_dir}/*{DEVICE}*.json")
    if json_f:
        files_to_upload.append(("JSON", json_f[0]))

    utils.upload_artifacts(final_build_msg, msg_id, files_to_upload, final_zip)


if __name__ == "__main__":
    main()
