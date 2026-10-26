"""ゲームごとのインストール容量（今の版を新しくインストールしたときの大きさ）をまとめる。

測り方はゲームごとに config/games.toml の [games.install] で指定する。
取得に失敗したときは、前回の値を「前回の値」として表示する。
"""
from __future__ import annotations

from datetime import datetime

from .common import FetchError, log, to_iso
from .sources import hoyoverse, riot_manifest

BASIS = {
    "steam": "Steam の配信データ（Windows・日本語で入るファイルの合計）",
    "hoyoplay": "HoYoPlay（公式ランチャー）の配信データ（本体＋日本語音声）",
    "steam+hoyo_voice": "Steam の配信データ＋HoYoPlay の日本語音声データ",
    "riot": "Riot の配信データ（日本サーバー・Windows・日本語で入るファイルの合計）",
    "official": "公式の必要容量",
}


def _steam_size(g: dict, steam_sizes: dict[int, int]) -> int:
    size = steam_sizes.get(g["steam"]["appid"])
    if not size:
        raise FetchError("Steam のインストール容量を取得できませんでした")
    return size


def _hoyo_sizes(g: dict, branches: dict) -> tuple[dict[str, int], str]:
    br = branches.get(g["hoyoverse"]["launcher_id"])
    if not br:
        raise FetchError("HoYoPlay の配信データがありません")
    sizes = hoyoverse.install_sizes(br["main"])
    if not sizes.get("ja-jp"):
        raise FetchError("HoYoPlay の配信データに日本語音声がありません")
    return sizes, str(br["main"].get("tag", ""))


def measure(method: str, g: dict, cfg: dict, steam_sizes: dict[int, int], branches: dict,
            prev: dict | None) -> dict:
    """戻り値: {"parts": [[名前, バイト数], ...], "version": 版（分かる場合）, ...}"""
    if method == "steam":
        return {"parts": [["本体", _steam_size(g, steam_sizes)]]}
    if method == "hoyoplay":
        sizes, tag = _hoyo_sizes(g, branches)
        return {"parts": [["本体", sizes["game"]], ["日本語音声", sizes["ja-jp"]]], "version": f"Ver.{tag}"}
    if method == "steam+hoyo_voice":
        steam = _steam_size(g, steam_sizes)
        sizes, tag = _hoyo_sizes(g, branches)
        return {"parts": [["Steam 版の本体", steam], ["日本語音声", sizes["ja-jp"]]]}
    if method == "riot":
        r = riot_manifest.install_size((prev or {}).get("riot"))
        return {"parts": r["parts"], "riot": r}  # 版の番号（16.21 など）は画面のパッチ名（26.21 など）と違うので出さない
    if method == "official":
        return {"parts": [["公式の必要容量", int(float(cfg["official_gb"]) * 1000**3)]]}
    raise FetchError(f"容量の測り方「{method}」は未対応です")


def collect(games: list[dict], steam_sizes: dict[int, int], branches: dict, store: dict,
            now: datetime) -> dict[str, dict]:
    """戻り値: ゲーム ID → 画面に出す容量の情報。store（state.json の一部）に計算結果を残す。"""
    out: dict[str, dict] = {}
    for g in games:
        cfg = g.get("install") or {}
        method = cfg.get("from")
        if not method:
            continue
        prev = store.get(g["id"])
        if prev and prev.get("from") != method:
            prev = None
        stale = False
        try:
            rec = measure(method, g, cfg, steam_sizes, branches, prev)
            rec["from"] = method
            if method != "official":
                rec["checked_at"] = to_iso(now)
            store[g["id"]] = rec
        except (FetchError, KeyError, ValueError) as e:
            log.warning("%s のインストール容量を取得できませんでした: %s", g["id"], e)
            if not prev:
                continue
            rec, stale = prev, True
        parts = rec["parts"]
        info = {
            "bytes": sum(p[1] for p in parts),
            "basis": BASIS[method],
            "official": method == "official",
        }
        if len(parts) > 1:
            info["parts"] = parts
        for key in ("version", "checked_at"):
            if rec.get(key):
                info[key] = rec[key]
        if cfg.get("note"):
            info["note"] = cfg["note"]
        if stale:
            info["stale"] = True
        out[g["id"]] = info
    return out
