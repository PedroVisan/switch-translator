import re
import pythoncom
import win32com.client


def get_switch_folder():
    # Necessário para chamadas COM funcionarem dentro de threads no Windows
    pythoncom.CoInitialize()
    shell = win32com.client.Dispatch("Shell.Application")
    this_pc = shell.NameSpace(17)  # 17 = "Este Computador"

    for item in this_pc.Items():
        if "switch" in item.Name.lower():
            return item.GetFolder
    return None


def list_installed_games():
    switch_folder = get_switch_folder()
    if not switch_folder:
        return None  # Switch não conectado ou DBI MTP fechado

    games = []
    for sub in switch_folder.Items():
        if "installed games" in sub.Name.lower():
            for game_item in sub.GetFolder.Items():
                raw_name = game_item.Name
                match = re.search(r"\[([0-9A-Fa-f]{16})\]", raw_name)
                title_id = match.group(1).upper() if match else "SEM_ID"
                games.append({
                    "name": raw_name,
                    "title_id": title_id,
                })
            break
    return games