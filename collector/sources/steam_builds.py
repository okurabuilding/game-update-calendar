"""Steam の公開ビルド（public ブランチ）の番号と更新時刻を取得する。

ゲーム本体のデータが更新されると、ビルド番号と更新時刻（秒単位）が変わる。
これを「ダウンロードが発生した」ことの確定判定に使う。

取得方法:
  1. SteamCMD（Valve 公式のコマンドラインツール）。GitHub Actions ではこちらを使う。
  2. 1 が使えない場合は api.steamcmd.net（SteamCMD と同じ情報を返す Web API）。
"""
from __future__ import annotations

import re
import subprocess
import time

from ..common import FetchError, log, request_json

_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict:
    """Valve の KeyValues（VDF）形式を辞書に変換する。"""
    tokens = [(m.group(1), m.group(2)) for m in _TOKEN_RE.finditer(text)]
    pos = 0

    def parse_obj() -> dict:
        nonlocal pos
        obj: dict = {}
        while pos < len(tokens):
            key, brace = tokens[pos]
            if brace == "}":
                pos += 1
                return obj
            if key is None:
                raise ValueError("VDF の形式が不正です")
            pos += 1
            if pos >= len(tokens):
                raise ValueError("VDF が途中で終わっています")
            val, vbrace = tokens[pos]
            if vbrace == "{":
                pos += 1
                obj[key] = parse_obj()
            elif val is not None:
                pos += 1
                obj[key] = val.replace('\\"', '"').replace("\\\\", "\\")
            else:
                raise ValueError("VDF の形式が不正です")
        return obj

    return parse_obj()


def extract_app_blocks(output: str, appids: list[int]) -> dict[int, dict]:
    """SteamCMD の出力から、各アプリの情報ブロック `"570" { ... }` を取り出す。"""
    result: dict[int, dict] = {}
    for appid in appids:
        m = re.search(r'^\s*"%d"\s*\n\s*\{' % appid, output, re.M)
        if not m:
            continue
        i = output.index("{", m.start())
        depth = 0
        while i < len(output):
            c = output[i]
            if c == '"':
                # 文字列の中の波かっこは数えない
                i += 1
                while i < len(output) and output[i] != '"':
                    i += 2 if output[i] == "\\" else 1
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    parsed = parse_vdf(output[m.start(): i + 1])
                    result[appid] = parsed.get(str(appid), {})
                    break
            i += 1
    return result


def _public_branch(info: dict) -> dict | None:
    branch = ((info.get("depots") or {}).get("branches") or {}).get("public")
    if not branch:
        return None
    try:
        buildid = int(branch["buildid"])
        updated = int(branch["timeupdated"])
    except (KeyError, TypeError, ValueError):
        return None
    now = int(time.time())
    if buildid <= 0 or not (1262304000 <= updated <= now + 86400):
        return None  # 2010年より前や未来の時刻は異常値として扱う
    return {"buildid": buildid, "timeupdated": updated}


def via_steamcmd(steamcmd: str, appids: list[int]) -> dict[int, dict]:
    args = [steamcmd, "+@ShutdownOnFailedCommand", "0", "+@NoPromptForPassword", "1",
            "+login", "anonymous", "+app_info_update", "1"]
    for appid in appids:
        args += ["+app_info_print", str(appid)]
    args.append("+quit")
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600)
    return extract_app_blocks(proc.stdout, appids)


def via_web_api(appid: int) -> dict | None:
    d = request_json(f"https://api.steamcmd.net/v1/info/{appid}", timeout=30)
    if d.get("status") != "success":
        return None
    return (d.get("data") or {}).get(str(appid)) or None


def install_size(info: dict, language: str = "japanese") -> int | None:
    """64 ビット版 Windows・指定の言語でインストールしたときの容量（バイト）。

    公開ブランチで配信中の各デポ（ファイルのまとまり）の展開後サイズを足し合わせる。
    DLC・他の言語・他の OS・任意の追加データ・共通の再頒布パッケージは含めない。
    """
    total, found = 0, False
    for key, dep in (info.get("depots") or {}).items():
        if not key.isdigit() or not isinstance(dep, dict):
            continue
        if dep.get("dlcappid") or dep.get("sharedinstall") or dep.get("depotfromapp"):
            continue
        cfg = dep.get("config") or {}
        if cfg.get("oslist") and "windows" not in cfg["oslist"].split(","):
            continue
        if cfg.get("osarch") == "32" or cfg.get("lowviolence") or cfg.get("optionaldlc"):
            continue
        if cfg.get("language") and cfg["language"] != language:
            continue
        if cfg.get("realm") and cfg["realm"] != "steamglobal":
            continue
        pub = (dep.get("manifests") or {}).get("public")
        if not isinstance(pub, dict) or "size" not in pub:
            continue
        try:
            total += int(pub["size"])
        except (TypeError, ValueError):
            return None
        found = True
    return total if found and total > 0 else None


def get_build_info(appids: list[int], steamcmd: str | None
                   ) -> tuple[dict[int, dict], dict[int, str], list[str], dict[int, int]]:
    """戻り値: (appid → {buildid, timeupdated}, appid → 取得元, 警告の一覧, appid → インストール容量)"""
    builds: dict[int, dict] = {}
    origin: dict[int, str] = {}
    warnings: list[str] = []
    sizes: dict[int, int] = {}
    if steamcmd:
        try:
            for appid, info in via_steamcmd(steamcmd, appids).items():
                b = _public_branch(info)
                if b:
                    builds[appid] = b
                    origin[appid] = "SteamCMD"
                    size = install_size(info)
                    if size:
                        sizes[appid] = size
        except (OSError, subprocess.SubprocessError) as e:
            warnings.append(f"SteamCMD の実行に失敗しました（{e}）。Web API で取得します")
        missing = [a for a in appids if a not in builds]
        if missing:
            log.info("SteamCMD で取得できなかったアプリ: %s", missing)
            warnings.append(f"SteamCMD で取得できなかったアプリ（{', '.join(map(str, missing))}）は Web API で取得しました")
    for appid in appids:
        if appid in builds and appid in sizes:
            continue
        try:
            info = via_web_api(appid)
        except FetchError as e:
            if appid not in builds:
                warnings.append(f"アプリ {appid} のビルド情報を取得できませんでした（{e}）")
            continue
        b = _public_branch(info) if info else None
        if appid not in builds:
            if not b:
                warnings.append(f"アプリ {appid} のビルド情報が見つかりませんでした")
                continue
            builds[appid] = b
            origin[appid] = "api.steamcmd.net"
        # 容量だけを Web API で補う場合は、SteamCMD と同じビルドの情報であることを確かめる
        if b and b["buildid"] == builds[appid]["buildid"]:
            size = install_size(info)
            if size:
                sizes[appid] = size
    return builds, origin, warnings, sizes
