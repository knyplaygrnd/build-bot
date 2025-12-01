import os
import sys
import time
import subprocess
import shutil
import argparse
import requests
import html
import signal
import json
import base64
import re
from datetime import datetime

# Config
if os.path.exists("config.env"):
    with open("config.env", "r") as f:
        for line_content in f:
            if "=" in line_content and not line_content.strip().startswith("#"):
                k, v = line_content.strip().split("=", 1)
                os.environ[k] = v.strip('"').strip("'")

BOT_TOKEN = os.environ.get("CONFIG_BOT_TOKEN")
CHAT_ID = os.environ.get("CONFIG_CHATID")

if not BOT_TOKEN or not CHAT_ID:
    print("ERROR: CONFIG_BOT_TOKEN or CONFIG_CHATID missing.")
    sys.exit(1)

ERROR_CHAT_ID = os.environ.get("CONFIG_ERROR_CHATID", CHAT_ID)

# Kernel Specific
DEFCONFIG = os.environ.get("CONFIG_DEFCONFIG")
AK3_REPO = os.environ.get("CONFIG_AK3_REPO")

# Upload
PD_API = os.environ.get("CONFIG_PDUP_API")
USE_GOFILE = os.environ.get("CONFIG_GOFILE") == "true"

if not DEFCONFIG:
    print("ERROR: Missing kernel configuration (CONFIG_DEFCONFIG).")
    sys.exit(1)

cpu_cores = os.cpu_count()
jobs_env = os.environ.get("CONFIG_JOBS")
JOBS_FLAG = f"-j{jobs_env}" if jobs_env else (f"-j{cpu_cores}" if cpu_cores else "-j4")
DISPLAY_JOBS = jobs_env if jobs_env else (f"{cpu_cores} (All)" if cpu_cores else "4")

# Directories
KERNEL_OUT = "out/arch/arm64/boot"
ANYKERNEL_DIR = "AnyKernel3"
LOG_FILE = "build.log"

# Global process handle
BUILD_PROCESS = None


def signal_handler(sig, frame):
    global BUILD_PROCESS
    print("\n[BOT] Interruption detected. Exiting...")
    if BUILD_PROCESS and BUILD_PROCESS.poll() is None:
        print("[BOT] Killing build process...")
        BUILD_PROCESS.terminate()
        time.sleep(1)
        if BUILD_PROCESS.poll() is None:
            BUILD_PROCESS.kill()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)


# Helpers
def fmt_time(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


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
        cmd = "clang --version | head -n 1"
        output = subprocess.check_output(cmd, shell=True, text=True).strip()
        match = re.search(r"clang version \d+\.\d+\.\d+", output)
        if match:
            return match.group(0)
        if "clang version" in output:
            return "Clang " + output.split("clang version")[-1].strip().split()[0]
    except Exception:
        pass
    return "Clang/LLVM"


def get_compiled_version_string():
    # Extracts the full version from the kernel image.
    image_path = os.path.join(KERNEL_OUT, "Image")
    if not os.path.exists(image_path):
        return None
    try:
        cmd = f"strings {image_path} | grep 'Linux version [0-9]' | head -n 1"
        line_out = subprocess.check_output(cmd, shell=True, text=True).strip()

        match = re.search(r"Linux version (\S+)", line_out)
        if match:
            return match.group(1)
    except Exception as e:
        print(f"Version Extraction Error: {e}")
        pass
    return None


def package_anykernel(version_string):
    print("Packaging AnyKernel3...")
    if os.path.exists(ANYKERNEL_DIR):
        shutil.rmtree(ANYKERNEL_DIR)

    subprocess.call(["git", "clone", "-q", AK3_REPO, ANYKERNEL_DIR])

    files_map = {"Image.gz": "Image.gz", "dtbo.img": "dtbo.img", "dtb.img": "dtb"}

    for src_name, dst_name in files_map.items():
        src = os.path.join(KERNEL_OUT, src_name)
        dst = os.path.join(ANYKERNEL_DIR, dst_name)
        if os.path.exists(src):
            shutil.copy(src, dst)

    timestamp = datetime.now().strftime("%Y%a%b%d-%H%M%S")

    # Use extracted string from binary (strings output) for accuracy
    ver_tag = version_string if version_string else "Unknown-Kernel"

    zip_name = f"KSU-{ver_tag}-{timestamp}.zip"

    cwd = os.getcwd()
    os.chdir(ANYKERNEL_DIR)
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


def tg_req(method, data, files=None, retries=3):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for attempt in range(retries):
        try:
            r = requests.post(url, data=data, files=files, timeout=30)
            if r.status_code == 200:
                return r.json()
            print(f"[Telegram Error {r.status_code}] {r.text}")
        except Exception as e:
            print(f"[Telegram Retry {attempt+1}/{retries}] {e}")
            time.sleep(2)
    return {}


def send_msg(text, chat=CHAT_ID, buttons=None):
    data = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "html",
        "disable_web_page_preview": "true",
    }
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return tg_req("sendMessage", data).get("result", {}).get("message_id")


def edit_msg(msg_id, text, chat=CHAT_ID, buttons=None):
    if not msg_id:
        return
    data = {
        "chat_id": chat,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "html",
        "disable_web_page_preview": "true",
    }
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    tg_req("editMessageText", data)


def send_doc(file_path, chat=CHAT_ID):
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            tg_req(
                "sendDocument",
                {"chat_id": chat, "parse_mode": "html"},
                files={"document": f},
            )


def line(label, value):
    return f"<b>{label}:</b> <code>{html.escape(str(value))}</code>"


def format_msg(icon, title, details, footer=""):
    header = f"<b>{icon} | {title}</b>"
    msg = f"{header}\n{details}"
    if footer:
        msg += f"\n\n<i>{html.escape(footer)}</i>"
    return msg


def upload_pd(path):
    print(f"Uploading to PixelDrain: {path}")
    if not PD_API:
        print("PixelDrain API key missing.")
        return None

    file_name = os.path.basename(path)
    url = f"https://pixeldrain.com/api/file/{file_name}"

    auth_str = f":{PD_API}"
    auth_bytes = auth_str.encode("ascii")
    base64_auth = base64.b64encode(auth_bytes).decode("ascii")

    headers = {"Authorization": f"Basic {base64_auth}"}

    try:
        with open(path, "rb") as f:
            r = requests.put(url, data=f, headers=headers, timeout=300)

        if r.status_code == 200:
            return f"https://pixeldrain.com/u/{r.json().get('id')}"
        elif r.status_code == 201:
            return f"https://pixeldrain.com/u/{r.json().get('id')}"

        print(f"[PixelDrain Error {r.status_code}] {r.text}")
        return None
    except Exception as e:
        print(f"PixelDrain Upload Error: {e}")
        return None


def upload_gofile(path):
    print(f"Uploading to GoFile: {path}")
    try:
        server_req = requests.get("https://api.gofile.io/servers")
        server = server_req.json()["data"]["servers"][0]["name"]
        r = requests.post(
            f"https://{server}.gofile.io/uploadFile",
            files={"file": open(path, "rb")},
            timeout=300,
        )
        if r.status_code == 200:
            return r.json()["data"]["downloadPage"]
        return None
    except Exception as e:
        print(f"GoFile Upload Error: {e}")
        return None


# Main
def main():
    global BUILD_PROCESS

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--clean", action="store_true")
    args = parser.parse_args()

    if args.clean:
        print("Cleaning out/...")
        if os.path.exists("out"):
            shutil.rmtree("out")

    git_head_link = get_git_head()
    compiler_ver = get_compiler_version()

    head_line = f"<b>head:</b> <code>{git_head_link}</code>"

    base_info = (
        f"{head_line}\n"
        f"{line('defconfig', DEFCONFIG)}\n"
        f"{line('jobs', DISPLAY_JOBS)}\n"
        f"{line('compiler', compiler_ver)}"
    )

    msg_id = send_msg(format_msg("ℹ️", "Starting...", base_info))

    print(f"Configuring: {DEFCONFIG}")
    subprocess.call(
        f"make O=out ARCH=arm64 LLVM=1 {DEFCONFIG}", shell=True, executable="/bin/bash"
    )

    local_ver = get_localversion()

    base_info = (
        f"{head_line}\n"
        f"{line('local version', local_ver)}\n"
        f"{line('defconfig', DEFCONFIG)}\n"
        f"{line('jobs', DISPLAY_JOBS)}\n"
        f"{line('compiler', compiler_ver)}"
    )

    build_cmd = f"make {JOBS_FLAG} O=out ARCH=arm64 LLVM=1 Image.gz dtbo.img dtb.img"
    print(f"Building: {build_cmd}")

    start_time = time.time()
    log_file = open(LOG_FILE, "w")

    BUILD_PROCESS = subprocess.Popen(
        build_cmd,
        shell=True,
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    last_update = 0

    try:
        while True:
            line_out = BUILD_PROCESS.stdout.readline()
            if not line_out and BUILD_PROCESS.poll() is not None:
                break
            if not line_out:
                time.sleep(0.1)
                continue

            log_file.write(line_out)

            now = time.time()
            if now - last_update > 10:
                elapsed = fmt_time(now - start_time)

                status_details = f"{line('elapsed', elapsed)}\n\n" f"{base_info}"

                edit_msg(
                    msg_id,
                    format_msg(
                        "🔄", "Building...", status_details, "Check logs for output"
                    ),
                )

                sys.stdout.write(f"\r[Building] {elapsed}...")
                sys.stdout.flush()
                last_update = now

        return_code = BUILD_PROCESS.poll()
        if return_code is None:
            return_code = BUILD_PROCESS.wait()

    except Exception as e:
        print(f"Build Loop Error: {e}")
        return_code = 1
    finally:
        log_file.close()
        sys.stdout.write("\n")

    total_duration = fmt_time(time.time() - start_time)

    if return_code != 0:
        fail_details = f"{line('duration', total_duration)}\n\n{base_info}"
        edit_msg(msg_id, format_msg("⚠️", "Build Failed", fail_details))
        send_doc(LOG_FILE, ERROR_CHAT_ID)
        sys.exit(1)

    transition_msg = (
        f"<b>✅ | Build Complete</b>\n"
        f"{base_info}\n\n"
        f"<b>☁️ | Packaging & Uploading...</b>"
    )
    edit_msg(msg_id, transition_msg)

    compiled_ver_str = get_compiled_version_string()

    final_zip = package_anykernel(compiled_ver_str)

    if not final_zip:
        edit_msg(msg_id, format_msg("⚠️", "Packaging Failed", "Could not create ZIP."))
        sys.exit(1)

    file_name = os.path.basename(final_zip)
    upload_start = time.time()
    pd_link = upload_pd(final_zip)
    gf_link = upload_gofile(final_zip) if USE_GOFILE else None
    upload_duration = fmt_time(time.time() - upload_start)

    size_mb = os.path.getsize(final_zip) / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB"
    try:
        md5 = subprocess.check_output(["md5sum", final_zip], text=True).split()[0]
    except:
        md5 = "N/A"

    final_combined_msg = (
        f"<b>✅ | Build Complete</b>\n"
        f"{head_line}\n"
        f"{line('local version', local_ver)}\n"
        f"{line('defconfig', DEFCONFIG)}\n"
        f"{line('jobs', DISPLAY_JOBS)}\n"
        f"{line('compiler', compiler_ver)}\n\n"
        f"<b>✅ | Upload Complete</b>\n"
        f"{line('build time', total_duration)}\n"
        f"{line('upload time', upload_duration)}\n\n"
        f"{line('file', file_name)}\n"
        f"{line('size', size_str)}\n"
        f"{line('md5', md5)}"
    )

    buttons_list = []
    if pd_link:
        buttons_list.append({"text": "PixelDrain", "url": pd_link})
    if USE_GOFILE and gf_link:
        buttons_list.append({"text": "GoFile", "url": gf_link})

    if buttons_list:
        edit_msg(msg_id, final_combined_msg, buttons=[buttons_list])
    else:
        edit_msg(msg_id, final_combined_msg)

    print("=== Finished ===")


if __name__ == "__main__":
    main()
