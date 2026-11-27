import re
import pythoncom
import win32com.client

TITLE_ID_RE = re.compile(r"\[([0-9A-Fa-f]{16})\]")


def list_installed_games() -> list[dict] | None:
    pythoncom.CoInitialize()
    shell = win32com.client.Dispatch("Shell.Application")
    my_computer = shell.NameSpace(17)
    if not my_computer:
        return None

    installed_games_folder = None
    for item in my_computer.Items():
        path = getattr(item, "Path", "") or ""
        if len(path) <= 3 and ":" in path:
            continue
        try:
            dev_folder = item.GetFolder
            if not dev_folder:
                continue
            for sub in dev_folder.Items():
                if sub.Name.strip().lower() == "installed games":
                    installed_games_folder = sub.GetFolder
                    break
        except Exception:
            continue
        if installed_games_folder:
            break

    if not installed_games_folder:
        return None

    games = []
    for game_item in installed_games_folder.Items():
        raw_name = game_item.Name.strip()
        match = TITLE_ID_RE.search(raw_name)
        tid = match.group(1).upper() if match else "SEM_ID"
        games.append({"name": raw_name, "title_id": tid})

    return games