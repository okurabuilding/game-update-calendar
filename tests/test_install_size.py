"""インストール容量の計算のテスト。"""
import unittest
from datetime import datetime
from unittest import mock

from collector import install_size
from collector.common import JST
from collector.sources import riot_manifest, steam_builds

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=JST)


def depot(size=None, **cfg):
    d = {"config": cfg} if cfg else {}
    if size is not None:
        d["manifests"] = {"public": {"gid": "1", "size": str(size), "download": str(size // 2)}}
    return d


class SteamInstallSizeTest(unittest.TestCase):
    def test_counts_only_windows_japanese_depots(self):
        info = {"depots": {
            "101": depot(1000),                              # 共通
            "102": depot(200, oslist="windows"),             # Windows 用
            "103": depot(300, oslist="macos"),               # Mac 用 → 除く
            "104": depot(40, language="japanese"),           # 日本語 → 含む
            "105": depot(50, language="english"),            # 英語 → 除く
            "106": depot(60, lowviolence="1"),               # 表現規制版 → 除く
            "107": {**depot(70, optionaldlc="9"), "dlcappid": "9"},  # DLC → 除く
            "108": {**depot(80, oslist="windows"), "sharedinstall": "1"},  # 共通の再頒布パッケージ → 除く
            "109": depot(None, oslist="windows"),            # 公開版のファイルなし → 除く
            "branches": {"public": {"buildid": "5", "timeupdated": "1790000000"}},
        }}
        self.assertEqual(steam_builds.install_size(info), 1000 + 200 + 40)

    def test_none_when_no_sizes(self):
        self.assertIsNone(steam_builds.install_size({"depots": {"101": {"manifests": {"public": "123"}}}}))


class RiotLocaleSizeTest(unittest.TestCase):
    def test_common_plus_japanese(self):
        langs = {1: "windows", 10: "en_US", 19: "ja_JP"}
        win, en, ja = 1 << 0, 1 << 9, 1 << 18
        files = [
            ("Map11.wad.client", 1000, 0),          # 共通
            ("Launcher.dll", 100, win),             # Windows 用（言語ではない印）→ 含む
            ("Ahri.en_US.wad.client", 30, en),      # 英語 → 除く
            ("Ahri.ja_JP.wad.client", 40, ja),      # 日本語 → 含む
            ("Both.wad.client", 5, en | ja),        # 英語と日本語の両方 → 含む
        ]
        self.assertEqual(riot_manifest.locale_size(langs, files), 1000 + 100 + 40 + 5)

    def test_missing_locale_is_error(self):
        with self.assertRaises(riot_manifest.FetchError):
            riot_manifest.locale_size({1: "en_US"}, [("a", 1, 0)])

    def test_rejects_unknown_format(self):
        with self.assertRaises(riot_manifest.FetchError):
            riot_manifest.parse_manifest(b"NOTRMAN")


class CollectTest(unittest.TestCase):
    games = [
        {"id": "A", "steam": {"appid": 1}, "install": {"from": "steam"}},
        {"id": "B", "install": {"from": "official", "official_gb": 30, "note": "注記"}},
        {"id": "C", "hoyoverse": {"launcher_id": "x"}, "install": {"from": "hoyoplay"}},
        {"id": "D", "name": "測り方の指定なし"},
    ]

    def test_measure_and_output(self):
        store = {}
        with mock.patch.object(install_size.hoyoverse, "install_sizes",
                               return_value={"game": 100, "ja-jp": 20, "en-us": 25}):
            out = install_size.collect(self.games, {1: 500}, {"x": {"main": {"tag": "7.1.0"}}}, store, NOW)
        self.assertEqual(out["A"]["bytes"], 500)
        self.assertNotIn("parts", out["A"])
        self.assertEqual(out["B"]["bytes"], 30 * 1000**3)
        self.assertTrue(out["B"]["official"])
        self.assertEqual(out["B"]["note"], "注記")
        self.assertNotIn("checked_at", out["B"])
        self.assertEqual(out["C"]["bytes"], 120)
        self.assertEqual(out["C"]["parts"], [["本体", 100], ["日本語音声", 20]])
        self.assertEqual(out["C"]["version"], "Ver.7.1.0")
        self.assertNotIn("D", out)
        self.assertEqual(store["A"]["checked_at"], "2026-10-26T12:00+09:00")

    def test_failure_keeps_previous_value(self):
        store = {"A": {"from": "steam", "parts": [["本体", 400]], "checked_at": "2026-10-25T12:00+09:00"}}
        with mock.patch.object(install_size.hoyoverse, "install_sizes",
                               side_effect=install_size.FetchError("失敗")):
            out = install_size.collect(self.games, {}, {}, store, NOW)
        self.assertEqual(out["A"]["bytes"], 400)
        self.assertTrue(out["A"]["stale"])
        self.assertEqual(out["A"]["checked_at"], "2026-10-25T12:00+09:00")
        self.assertNotIn("C", out)  # 前回の値もないときは載せない


if __name__ == "__main__":
    unittest.main()
