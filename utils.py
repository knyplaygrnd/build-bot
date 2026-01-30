import os
import sys
import time
import json
import html
import base64
import requests
import signal
import subprocess
import re
import concurrent.futures
from dotenv import load_dotenv

# Load configs from .env file
load_dotenv("config.env")


class Config:
    def __init__(self):
        self.BOT_TOKEN = os.environ.get("CONFIG_BOT_TOKEN")
        self.CHAT_ID = os.environ.get("CONFIG_CHATID")
        self.ERROR_CHAT_ID = os.environ.get("CONFIG_ERROR_CHATID", self.CHAT_ID)
        self.PD_API = os.environ.get("CONFIG_PDUP_API")
        self.USE_GOFILE = os.environ.get("CONFIG_GOFILE") == "true"


config = Config()

# Message templates for Telegram notifications
MESSAGES = {
    "sync_start": "<b>ℹ️ | Starting Synchronization...</b>\n{details}",
    "sync_done": "<b>✅ | Synchronization Complete!</b>\n{details}\n<b>Time:</b> {dur}",
    "build_start": "<b>ℹ️ | Starting Build...</b>\n\n{base_info}",
    "build_progress": ("<b>🔄 | Building...</b>\n" "{stats}\n\n" "{base_info}"),
    "build_fail": "<b>⚠️ | Build Failed</b>\n\nFailed after {time}\n\n{base_info}",
    "build_success": (
        "<b>✅ | Build Complete!</b>\n"
        "<b>Build Time:</b> <code>{time}</code>\n\n"
        "{base_info}"
    ),
    "uploading": "{build_msg}\n\n<b>🔄 | Uploading Files...</b>",
    "upload_fail": "{build_msg}\n\n<b>⚠️ | Upload Failed</b>\n\n{reason}",
    "final_msg": (
        "{build_msg}\n\n"
        "<b>✅ | Upload Complete</b>\n"
        "<b>Upload Time:</b> <code>{up_time}</code>\n\n"
        "<b>File:</b> <code>{filename}</code>\n"
        "<b>Size:</b> <code>{size}</code>\n"
        "<b>MD5:</b> <code>{md5}</code>"
    ),
}


def get_jobs_flag():
    cpu_cores = os.cpu_count()
    jobs_env = os.environ.get("CONFIG_JOBS")
    return f"-j{jobs_env}" if jobs_env else (f"-j{cpu_cores}" if cpu_cores else "-j4")


def get_sync_jobs():
    cpu_cores = os.cpu_count()
    jobs_env = os.environ.get("CONFIG_JOBS")
    return jobs_env if jobs_env else (str(cpu_cores) if cpu_cores else "4")


class BuildRunner:
    def __init__(self, build_cmd, base_info, log_file="build.log", is_rom=False):
        self.build_cmd = build_cmd
        self.base_info = base_info
        self.log_file = log_file
        self.is_rom = is_rom
        self.process = None
        self.msg_id = None

    def run(self):
        register_signal_handler(lambda: self.process)
        self.msg_id = send_msg(MESSAGES["build_start"].format(base_info=self.base_info))

        start_time = time.time()
        with open(self.log_file, "w") as log:
            self.process = subprocess.Popen(
                self.build_cmd,
                shell=self.is_rom,
                executable="/bin/bash" if self.is_rom else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            last_update = 0
            ninja_started = not self.is_rom
            regex = re.compile(r"\[\s*(\d+%)\s+(\d+/\d+)(?: (.*?remaining))?.*\]")

            try:
                for line_out in self.process.stdout:
                    sys.stdout.write(line_out)
                    log.write(line_out)

                    if self.is_rom:
                        if "Starting ninja..." in line_out:
                            ninja_started = True

                        match = regex.search(line_out)
                        if match and ninja_started:
                            pct, cnt, time_left = match.groups()
                            now = time.time()
                            if now - last_update > 15:
                                elapsed_str = fmt_time(now - start_time)
                                stats_str = (
                                    f"<b>Progress:</b> <code>{pct} ({cnt})</code>\n"
                                )
                                if time_left:
                                    clean_time = time_left.replace(
                                        " remaining", ""
                                    ).strip()
                                    stats_str += (
                                        f"<b>Remaining:</b> <code>{clean_time}</code>\n"
                                    )
                                stats_str += (
                                    f"<b>Elapsed:</b> <code>{elapsed_str}</code>"
                                )
                                edit_msg(
                                    self.msg_id,
                                    MESSAGES["build_progress"].format(
                                        stats=stats_str, base_info=self.base_info
                                    ),
                                )
                                last_update = now
                    else:
                        now = time.time()
                        if now - last_update > 15:
                            elapsed = fmt_time(now - start_time)
                            stats_str = f"<b>Elapsed:</b> <code>{elapsed}</code>"
                            edit_msg(
                                self.msg_id,
                                MESSAGES["build_progress"].format(
                                    stats=stats_str, base_info=self.base_info
                                ),
                            )
                            last_update = now

                return_code = self.process.wait()
            except Exception as e:
                print(f"Build Loop Error: {e}")
                return_code = 1

        total_duration = fmt_time(time.time() - start_time)

        if return_code != 0:
            edit_msg(
                self.msg_id,
                MESSAGES["build_fail"].format(
                    time=total_duration, base_info=self.base_info
                ),
            )
            error_log = (
                "out/error.log"
                if self.is_rom and os.path.exists("out/error.log")
                else self.log_file
            )
            send_doc(error_log, config.ERROR_CHAT_ID)
            sys.exit(1)

        final_build_msg = MESSAGES["build_success"].format(
            time=total_duration, base_info=self.base_info
        )
        edit_msg(self.msg_id, MESSAGES["uploading"].format(build_msg=final_build_msg))
        return final_build_msg, self.msg_id


def upload_artifacts(final_build_msg, msg_id, files_to_upload, final_zip):
    upload_start = time.time()
    buttons_list = []
    main_file_uploaded = False

    for label, file_path in files_to_upload:
        if not file_path or not os.path.exists(file_path):
            continue

        print(f"Uploading {label} ({os.path.basename(file_path)})...")
        uploads = upload_all(file_path, config.USE_GOFILE)

        current_row = []
        if uploads["pd"]:
            current_row.append({"text": f"{label} (PD)", "url": uploads["pd"]})
            if file_path == final_zip:
                main_file_uploaded = True

        if uploads["gf"]:
            current_row.append({"text": f"{label} (GF)", "url": uploads["gf"]})
            if file_path == final_zip:
                main_file_uploaded = True

        if current_row:
            buttons_list.append(current_row)

    upload_duration = fmt_time(time.time() - upload_start)

    if not main_file_uploaded:
        edit_msg(
            msg_id,
            MESSAGES["upload_fail"].format(
                build_msg=final_build_msg, reason="Could not upload files."
            ),
        )
        return

    md5 = get_md5(final_zip)
    size_mb = os.path.getsize(final_zip) / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB"
    file_name = os.path.basename(final_zip)

    edit_msg(
        msg_id,
        MESSAGES["final_msg"].format(
            build_msg=final_build_msg,
            up_time=upload_duration,
            filename=file_name,
            size=size_str,
            md5=md5,
        ),
        buttons=buttons_list if buttons_list else None,
    )


# Formatting
def fmt_time(seconds):
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def line(label, value):
    return f"<b>{label}:</b> <code>{html.escape(str(value))}</code>"


def get_md5(file_path):
    if not os.path.exists(file_path):
        return "N/A"
    try:
        return subprocess.check_output(["md5sum", file_path], text=True).split()[0]
    except Exception:
        return "N/A"


# Telegram API
def tg_req(method, data, files=None, retries=3):
    if not config.BOT_TOKEN:
        print("Error: BOT_TOKEN missing in utils.")
        return {}

    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
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


def _get_tg_payload(chat_id, text, buttons=None, msg_id=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "html",
        "disable_web_page_preview": "true",
    }
    if msg_id:
        data["message_id"] = msg_id
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return data


def send_msg(text, chat_id=config.CHAT_ID, buttons=None):
    if not chat_id:
        return None
    data = _get_tg_payload(chat_id, text, buttons)
    return tg_req("sendMessage", data).get("result", {}).get("message_id")


def edit_msg(msg_id, text, chat_id=config.CHAT_ID, buttons=None):
    if not msg_id or not chat_id:
        return
    data = _get_tg_payload(chat_id, text, buttons, msg_id)
    tg_req("editMessageText", data)


def send_doc(file_path, chat_id=config.CHAT_ID):
    if not chat_id:
        return
    if os.path.exists(file_path):
        with open(file_path, "rb") as f:
            tg_req(
                "sendDocument",
                {"chat_id": chat_id, "parse_mode": "html"},
                files={"document": f},
            )


# Upload for PixelDrain
def upload_pd(path):
    print(f"Uploading to PixelDrain: {path}")
    if not config.PD_API:
        print("PixelDrain API key missing.")
        return None

    file_name = os.path.basename(path)
    url = f"https://pixeldrain.com/api/file/{file_name}"

    auth_str = f":{config.PD_API}"
    auth_bytes = auth_str.encode("ascii")
    base64_auth = base64.b64encode(auth_bytes).decode("ascii")

    headers = {"Authorization": f"Basic {base64_auth}"}

    try:
        with open(path, "rb") as f:
            r = requests.put(url, data=f, headers=headers, timeout=300)

        if r.status_code in [200, 201]:
            return f"https://pixeldrain.com/u/{r.json().get('id')}"

        print(f"[PixelDrain Error {r.status_code}] {r.text}")
        return None
    except Exception as e:
        print(f"PixelDrain Upload Error: {e}")
        return None


# Upload for GoFile
def upload_gofile(path):
    print(f"Uploading to GoFile: {path}")
    try:
        server_req = requests.get("https://api.gofile.io/servers")
        data = server_req.json()
        if data["status"] != "ok":
            return None

        server = data["data"]["servers"][0]["name"]
        with open(path, "rb") as f:
            r = requests.post(
                f"https://{server}.gofile.io/uploadFile",
                files={"file": f},
                timeout=300,
            )
        if r.status_code == 200:
            return r.json()["data"]["downloadPage"]
        return None
    except Exception as e:
        print(f"GoFile Upload Error: {e}")
        return None


# Performs simultaneous uploads
def upload_all(path, use_gofile=False):
    results = {"pd": None, "gf": None}

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_pd = executor.submit(upload_pd, path)
        future_gf = executor.submit(upload_gofile, path) if use_gofile else None

        results["pd"] = future_pd.result()
        if future_gf:
            results["gf"] = future_gf.result()

    return results


# Signal handler to kill build processes
def register_signal_handler(process_getter):

    def handler(sig, frame):
        print("\n[BOT] Interruption detected. Exiting...")
        process = process_getter()

        if process and process.poll() is None:
            print("[BOT] Killing build process...")
            process.terminate()
            time.sleep(1)
            if process.poll() is None:
                process.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
