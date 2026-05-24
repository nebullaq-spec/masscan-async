import sys
import subprocess
import threading
import asyncio
import socket
import ctypes
import signal
import time
import struct
from itertools import islice
from collections import defaultdict

# Первичная установка зависимостей
def first_run_setup():
    required = ['colorama']
    print("[СИСТЕМА] Проверка зависимостей...")
    for package in required:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "show", package],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError:
            print(f"[СИСТЕМА] Установка {package}...")
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", package],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                print("[СИСТЕМА] Зависимости установлены. Перезапуск...")
                subprocess.call([sys.executable] + sys.argv)
                sys.exit(0)
            except subprocess.CalledProcessError as e:
                print(f"[ОШИБКА] Не удалось установить {package}: {e}")
                sys.exit(1)

first_run_setup()
from colorama import init, Fore, Style
init(autoreset=True)

# ── Настройки ─────────────────────────────────────────────────────────────────
PORTS          = [80, 8080, 443, 8443, 22]
CONCURRENCY    = 2000          # одновременных async-соединений (вместо THREADS)
RANGES_FILE    = "ranges.txt"
OUTPUT_DIR     = "results"     # папка для файлов результатов
TIMEOUT        = 1.5
UPDATE_INTERVAL = 0.15
BATCH_SIZE     = 1_000_000
autoclear_found_on_start = True
# ──────────────────────────────────────────────────────────────────────────────

COLOR_TITLE    = Fore.CYAN    + Style.BRIGHT
COLOR_HEADER   = Fore.MAGENTA + Style.BRIGHT
COLOR_SUCCESS  = Fore.GREEN   + Style.BRIGHT
COLOR_WARNING  = Fore.YELLOW  + Style.BRIGHT
COLOR_ERROR    = Fore.RED     + Style.BRIGHT
COLOR_PROGRESS = Fore.BLUE    + Style.BRIGHT

# Счётчики
checked_count    = 0
successful_count = 0
total_count      = 0
port_stats       = defaultdict(int)
last_found       = []
scan_start_time  = None

# Файловые дескрипторы: port -> file object, плюс общий
_file_handles    = {}   # {port: file} + {"all": file}

print_lock = threading.Lock()
stop_event = asyncio.Event()   # async-совместимый стоп
_loop      = None              # главный event loop

import os

def print_banner():
    banner = r"""
    ███╗   ███╗██╗   ██╗    ███████╗██████╗ ███████╗
    ████╗ ████║██║   ██║    ██╔════╝██╔══██╗██╔════╝
    ██╔████╔██║██║   ██║    █████╗  ██████╔╝███████╗
    ██║╚██╔╝██║╚██╗ ██╔╝    ██╔══╝  ██╔═══╝ ╚════██║
    ██║ ╚═╝ ██║ ╚████╔╝     ██║     ██║     ███████║
    ╚═╝     ╚═╝  ╚═══╝      ╚═╝     ╚═╝     ╚══════╝
    """
    print(COLOR_HEADER + banner)
    ports_str = ", ".join(str(p) for p in PORTS)
    print(COLOR_HEADER + " " * 10 +
          f"MV FastPortScanner v3.0 by qqwwddd https://t.me/nebullaq | Порты: {ports_str}\n")


def update_title():
    title = f"MV FPS | Проверено: {checked_count}/{total_count} | Открыто: {successful_count}"
    if sys.platform == 'win32':
        ctypes.windll.kernel32.SetConsoleTitleW(title)
    else:
        sys.stdout.write(f"\x1b]2;{title}\x07")


def _speed_str():
    if scan_start_time is None: return "─"
    elapsed = time.time() - scan_start_time
    if elapsed < 0.5: return "─"
    rate = checked_count / elapsed
    return f"{rate/1000:.1f}k/с" if rate >= 1000 else f"{rate:.0f}/с"


def _eta_str():
    if not scan_start_time or checked_count == 0: return "─"
    elapsed = time.time() - scan_start_time
    rate    = checked_count / elapsed
    if rate <= 0: return "─"
    rem = (total_count - checked_count) / rate
    h, m, s = int(rem // 3600), int((rem % 3600) // 60), int(rem % 60)
    if h > 0:  return f"{h}ч {m}м"
    if m > 0:  return f"{m}м {s}с"
    return f"{s}с"


PANEL_LINES = 0

def _build_panel():
    progress    = checked_count / total_count if total_count > 0 else 0
    failed      = checked_count - successful_count
    success_pct = (successful_count / checked_count * 100) if checked_count > 0 else 0
    W     = 72
    inner = W - 2

    filled = int((W - 4) * progress)
    empty  = (W - 4) - filled
    bar = (Fore.GREEN + Style.BRIGHT + "█" * filled +
           Fore.WHITE + Style.DIM    + "░" * empty  + Style.RESET_ALL)

    def row(c): return COLOR_HEADER + "│ " + c + Style.RESET_ALL

    sep = COLOR_HEADER + "├" + "─" * inner + "┤"
    top = COLOR_HEADER + "┌" + "─" * inner + "┐"
    bot = COLOR_HEADER + "└" + "─" * inner + "┘"

    lines = [
        top,
        row(f"{bar}  {COLOR_TITLE}{progress*100:5.1f}%"),
        sep,
        row(COLOR_PROGRESS + f"  Проверено : {checked_count:>10,} / {total_count:,}"),
        row(COLOR_SUCCESS  + f"  Открыто   : {successful_count:>10,}   " +
            COLOR_ERROR    + f"Закрыто : {failed:,}"),
        row(COLOR_WARNING  + f"  Успех     : {success_pct:>9.2f}%   " +
            COLOR_PROGRESS + f"Скорость: {_speed_str()}   " +
            COLOR_TITLE    + f"ETA: {_eta_str()}"),
    ]
    if last_found:
        lines.append(sep)
        lines.append(row(COLOR_SUCCESS + "  Последние находки:"))
        for entry in last_found:
            lines.append(row(COLOR_SUCCESS + f"    ✓  {entry}"))
    lines.append(bot)
    return lines


def _reserve_panel():
    global PANEL_LINES
    panel = _build_panel()
    PANEL_LINES = len(panel)
    sys.stdout.write("\n" * PANEL_LINES)
    sys.stdout.flush()
    _redraw_panel(panel)


def _redraw_panel(panel=None):
    if panel is None:
        panel = _build_panel()
    n = len(panel)
    with print_lock:
        sys.stdout.write(f"\x1b[{n}A\r")
        for line in panel:
            sys.stdout.write("\r\x1b[2K" + line + "\n")
        sys.stdout.flush()


# ── Файлы результатов ─────────────────────────────────────────────────────────
def init_output_files():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if autoclear_found_on_start:
        # очищаем все файлы портов + allresult
        for port in PORTS:
            open(os.path.join(OUTPUT_DIR, f"found_{port}.txt"), 'w').close()
        open(os.path.join(OUTPUT_DIR, "allresult.txt"), 'w').close()
        print(COLOR_SUCCESS + f"[✓] Файлы результатов очищены в папке '{OUTPUT_DIR}/'")

    _file_handles["all"] = open(os.path.join(OUTPUT_DIR, "allresult.txt"), 'a', buffering=1)
    for port in PORTS:
        _file_handles[port] = open(os.path.join(OUTPUT_DIR, f"found_{port}.txt"), 'a', buffering=1)


def close_output_files():
    for fh in _file_handles.values():
        try:
            fh.close()
        except Exception:
            pass


def _write_result(ip: str, port: int):
    """Пишет IP в found_{port}.txt и в allresult.txt. Потокобезопасно через GIL + buffering=1."""
    line = ip + "\n"
    _file_handles[port].write(line)
    _file_handles["all"].write(line)


# ── Async-ядро ────────────────────────────────────────────────────────────────
async def check_port(ip: str, port: int, semaphore: asyncio.Semaphore) -> tuple[str, int, bool]:
    async with semaphore:
        try:
            family = socket.AF_INET6 if ':' in ip else socket.AF_INET
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port, family=family),
                timeout=TIMEOUT
            )
            writer.close()
            await writer.wait_closed()
            return ip, port, True
        except Exception:
            return ip, port, False


async def result_handler(results_queue: asyncio.Queue):
    """Обрабатывает результаты в одной корутине — нет гонок на счётчиках."""
    global checked_count, successful_count
    while True:
        item = await results_queue.get()
        if item is None:          # сигнал завершения
            break
        ip, port, success = item
        checked_count += 1
        if success:
            successful_count += 1
            port_stats[port] += 1
            last_found.append(f"{ip}  (порт {port})")
            if len(last_found) > 4:
                last_found.pop(0)
            _write_result(ip, port)

        if checked_count % 100 == 0 or checked_count == total_count:
            _redraw_panel()
            update_title()


async def producer(tasks_queue: asyncio.Queue, semaphore: asyncio.Semaphore,
                   results_queue: asyncio.Queue):
    """Берёт (ip, port) из tasks_queue, запускает check_port, кладёт результат."""
    while True:
        item = await tasks_queue.get()
        if item is None:
            tasks_queue.task_done()
            break
        ip, port = item
        result = await check_port(ip, port, semaphore)
        await results_queue.put(result)
        tasks_queue.task_done()


async def run_scan(all_pairs: list):
    """Главная async-функция сканирования."""
    global scan_start_time
    scan_start_time = time.time()

    semaphore     = asyncio.Semaphore(CONCURRENCY)
    tasks_queue   = asyncio.Queue(maxsize=CONCURRENCY * 4)
    results_queue = asyncio.Queue()

    # Запускаем обработчик результатов
    handler = asyncio.create_task(result_handler(results_queue))

    # Запускаем CONCURRENCY воркеров
    workers = [
        asyncio.create_task(producer(tasks_queue, semaphore, results_queue))
        for _ in range(CONCURRENCY)
    ]

    # Кормим очередь
    for pair in all_pairs:
        await tasks_queue.put(pair)

    # Сигнал завершения — по одному None на каждый воркер
    for _ in range(CONCURRENCY):
        await tasks_queue.put(None)

    await tasks_queue.join()

    # Завершаем обработчик
    await results_queue.put(None)
    await handler

    # Ждём воркеров
    await asyncio.gather(*workers)


# ── Загрузка диапазонов ───────────────────────────────────────────────────────
def ipv4_range_to_ips(start: str, end: str):
    s = struct.unpack("!I", socket.inet_aton(start))[0]
    e = struct.unpack("!I", socket.inet_aton(end))[0]
    return (socket.inet_ntoa(struct.pack("!I", i)) for i in range(s, e + 1))


def batch_generator(gen, size):
    while True:
        batch = list(islice(gen, size))
        if not batch: break
        yield batch


def load_all_pairs() -> list:
    """Возвращает список (ip, port) для всех диапазонов."""
    global total_count
    pairs      = []
    loaded     = 0
    start_time = time.time()
    ipv6_count = 0

    try:
        with open(RANGES_FILE) as f:
            print(COLOR_HEADER + "[•] Загрузка диапазонов IP...")
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"): continue
                if '-' not in line:
                    print(COLOR_WARNING + f"[!] Пропуск строки {line_num}: {line}")
                    continue
                try:
                    start_str, end_str = map(str.strip, line.split('-', 1))
                    if ':' in start_str:
                        ipv6_count += 1
                        continue
                    gen = ipv4_range_to_ips(start_str, end_str)
                    for batch in batch_generator(gen, BATCH_SIZE):
                        for ip in batch:
                            for port in PORTS:
                                pairs.append((ip, port))
                        loaded += len(batch)
                        if time.time() - start_time > 1:
                            sys.stdout.write(COLOR_PROGRESS + f"\r[•] Загружено {loaded:,} IP")
                            sys.stdout.flush()
                except Exception as e:
                    print(COLOR_ERROR + f"[!] Ошибка в строке {line_num}: {e}")

    except FileNotFoundError:
        print(COLOR_ERROR + f"[!] Файл '{RANGES_FILE}' не найден!")
        sys.exit(1)

    total_count = len(pairs)
    duration    = max(time.time() - start_time, 0.01)
    print(COLOR_SUCCESS +
          f"\n[✓] Загружено {loaded:,} IP × {len(PORTS)} портов = {total_count:,} проверок "
          f"({loaded/duration:,.0f} IP/сек)")
    if ipv6_count:
        print(COLOR_WARNING + f"[•] Пропущено {ipv6_count} IPv6-диапазонов")
    print(COLOR_HEADER + "─" * 72)
    return pairs


# ── Финальная статистика ──────────────────────────────────────────────────────
def print_final_stats():
    sys.stdout.write("\n")
    elapsed = time.time() - (scan_start_time or time.time())
    print(COLOR_SUCCESS + "[✓] Сканирование завершено!")
    print(COLOR_HEADER   + "─" * 72)
    print(COLOR_TITLE    + f"Всего проверено : {checked_count:,}")
    print(COLOR_TITLE    + f"Открытых портов : {successful_count:,}")
    print(COLOR_TITLE    + f"Затрачено время : {elapsed:.1f} сек")
    if port_stats:
        print(COLOR_HEADER + "\nПо портам:")
        for port in PORTS:
            cnt = port_stats.get(port, 0)
            bar = Fore.GREEN + "█" * min(cnt, 30) + Style.RESET_ALL
            fname = f"found_{port}.txt"
            print(f"  :{port:<6}  {cnt:>6}  {bar}  → {fname}")
    print(COLOR_HEADER + "─" * 72)
    print(COLOR_TITLE  + f"Все результаты  : {OUTPUT_DIR}/allresult.txt")
    print(COLOR_TITLE  + f"По портам       : {OUTPUT_DIR}/found_<port>.txt")


# ── Точка входа ───────────────────────────────────────────────────────────────
def signal_handler(sig, frame):
    sys.stdout.write("\n")
    print(COLOR_ERROR + "[!] Остановка сканирования...")
    close_output_files()
    sys.exit(0)


def main():
    print_banner()
    signal.signal(signal.SIGINT, signal_handler)

    init_output_files()
    pairs = load_all_pairs()

    if not pairs:
        print(COLOR_WARNING + "[!] Нет задач для сканирования!")
        return

    print(COLOR_HEADER + f"[•] Запуск async-сканера | конкурентность: {CONCURRENCY}\n")
    _reserve_panel()

    try:
        asyncio.run(run_scan(pairs))
    except KeyboardInterrupt:
        signal_handler(None, None)

    close_output_files()
    print_final_stats()


if __name__ == "__main__":
    main()