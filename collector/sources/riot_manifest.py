"""LoL のインストール容量を、Riot の配信用ファイル一覧（マニフェスト）から計算する。

Riot Client は、次の 2 つのマニフェストに載っているファイルをダウンロードしてインストールする。
  - ロビー（League Client）… 日本サーバー用の設定に書かれたマニフェスト
  - ゲーム本体（Game）       … Riot の配信サービス（sieve）が返す、最新版のマニフェスト
マニフェストには、ファイルごとの大きさと「どの言語用か」の印が入っている。
共通のファイルと日本語用のファイルの大きさを足し合わせたものを、インストール容量とする。

マニフェストは zstd で圧縮されているため、Python 3.14 以上（標準ライブラリの compression.zstd）が必要。
"""
from __future__ import annotations

import struct

from ..common import FetchError, request_bytes, request_json

try:
    from compression import zstd
except ImportError:  # Python 3.13 以前
    zstd = None

CONFIG_URL = "https://clientconfig.rpg.riotgames.com/api/v1/config/public"
SIEVE_URL = "https://sieve.services.riotcdn.net/api/v1/products/lol/version-sets/JP1"
PATCHLINE = "keystone.products.league_of_legends.patchlines.live"


class _FlatBuffer:
    """マニフェストの中身（FlatBuffers 形式）を読むための最小限の処理。"""

    def __init__(self, data: bytes):
        self.b = data

    def u32(self, p: int) -> int:
        return struct.unpack_from("<I", self.b, p)[0]

    def field(self, table: int, index: int) -> int | None:
        vtable = table - struct.unpack_from("<i", self.b, table)[0]
        vsize = struct.unpack_from("<H", self.b, vtable)[0]
        if 4 + 2 * index >= vsize:
            return None
        off = struct.unpack_from("<H", self.b, vtable + 4 + 2 * index)[0]
        return table + off if off else None

    def tables(self, table: int, index: int) -> list[int]:
        p = self.field(table, index)
        if p is None:
            return []
        v = p + self.u32(p)
        return [v + 4 + 4 * k + self.u32(v + 4 + 4 * k) for k in range(self.u32(v))]

    def string(self, table: int, index: int) -> str:
        p = self.field(table, index)
        if p is None:
            return ""
        v = p + self.u32(p)
        return self.b[v + 4: v + 4 + self.u32(v)].decode("utf-8")

    def scalar(self, table: int, index: int, fmt: str) -> int:
        p = self.field(table, index)
        return struct.unpack_from(fmt, self.b, p)[0] if p is not None else 0


def parse_manifest(raw: bytes) -> tuple[dict[int, str], list[tuple[str, int, int]]]:
    """戻り値: (言語番号 → 言語名, [(ファイル名, 大きさ, 言語の印), ...])"""
    if raw[:4] != b"RMAN":
        raise FetchError("LoL のマニフェストの形式が想定と違います")
    if zstd is None:
        raise FetchError("LoL の容量の計算には Python 3.14 以上が必要です")
    offset, length = struct.unpack_from("<II", raw, 8)
    try:
        fb = _FlatBuffer(zstd.decompress(raw[offset: offset + length]))
        root = fb.u32(0)
        langs = {fb.scalar(t, 0, "<B"): fb.string(t, 1) for t in fb.tables(root, 1)}
        files = [(fb.string(t, 3), fb.scalar(t, 2, "<I"), fb.scalar(t, 4, "<Q")) for t in fb.tables(root, 2)]
    except (struct.error, IndexError, UnicodeDecodeError, zstd.ZstdError) as e:
        raise FetchError(f"LoL のマニフェストを読めませんでした（{e}）") from e
    if not files:
        raise FetchError("LoL のマニフェストにファイルがありません")
    return langs, files


def locale_size(langs: dict[int, str], files: list[tuple[str, int, int]], locale: str = "ja_JP") -> int:
    """共通のファイルと、指定した言語用のファイルの大きさの合計。"""
    bit = {name: 1 << (lid - 1) for lid, name in langs.items() if lid}
    if locale not in bit:
        raise FetchError(f"LoL のマニフェストに {locale} がありません")
    # 「windows」のように言語ではない印もあるので、言語名（xx_YY）の印だけで絞り込む
    loc_mask = sum(b for name, b in bit.items() if "_" in name)
    mine = bit[locale]
    return sum(size for _, size, flags in files if not flags & loc_mask or flags & mine)


def manifest_urls() -> tuple[str, str, str]:
    """戻り値: (ロビーのマニフェスト, ゲーム本体のマニフェスト, ゲーム本体のバージョン)"""
    cfg = request_json(CONFIG_URL, params={"namespace": PATCHLINE.rsplit(".", 1)[0]})
    try:
        win = cfg[PATCHLINE]["platforms"]["win"]
        jp = next(c for c in win["configurations"] if c["id"] == "JP")
        client = next(a["patch_url"] for a in jp["patch_artifacts"] if a["id"] == "league_client")
    except (KeyError, TypeError, StopIteration) as e:
        raise FetchError("LoL の配信設定に日本サーバーの情報がありません") from e
    sieve = request_json(SIEVE_URL, params={"q[platform]": "windows", "q[artifact_type_id]": "lol-game-client"})
    try:
        rel = sieve["releases"][0]
        game = rel["download"]["url"]
        version = rel["release"]["labels"]["riot:artifact_version_id"]["values"][0].split("+")[0]
    except (KeyError, TypeError, IndexError) as e:
        raise FetchError("LoL のゲーム本体の配信情報を読めませんでした") from e
    return client, game, version


def install_size(cached: dict | None) -> dict:
    """日本サーバー・Windows・日本語でのインストール容量。

    マニフェストが前回と同じなら、ダウンロードし直さずに前回の計算結果を使う。
    戻り値: {"key": マニフェストの組, "version": 版, "parts": [[名前, バイト数], ...]}
    """
    client, game, version = manifest_urls()
    key = f"{client}|{game}"
    if cached and cached.get("key") == key and cached.get("parts"):
        return cached
    parts = []
    for label, url in (("ロビー画面", client), ("ゲーム本体", game)):
        langs, files = parse_manifest(request_bytes(url, timeout=120))
        parts.append([label, locale_size(langs, files)])
    return {"key": key, "version": version, "parts": parts}
