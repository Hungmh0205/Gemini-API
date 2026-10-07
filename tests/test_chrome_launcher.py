import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

if (_root := str(Path(__file__).resolve().parent.parent)) not in sys.path:
    sys.path.insert(0, _root)

import chrome_launcher as chrome_launcher_mod
from chrome_launcher import (
    BASE_DIR,
    DEFAULT_AGENT_PROFILE,
    DEFAULT_WORK_DIR,
    EXT_DIR,
    EXT_NAME,
    _clone_state,
    _norm,
    _parse_profile_directory,
    _parse_user_data_dirs,
    _robocopy_args,
    build_chrome_command,
    browser_pids,
    chrome_data_in_use,
    chrome_instance_alive,
    close_agent_chrome,
    clone_profile,
    discover_profiles,
    extension_id_from_path,
    extension_installed_in,
    extension_registration,
    find_profiles_with_extension,
    list_profiles,
    prepare_work_profile,
    profile_running_pids,
    resolve_agent_profile,
    resolve_profile,
    resolve_template_profile,
    resolve_work_dir,
)
from chrome_launcher import ChromeProfile
from server import _is_extension_client


def _write_secure_prefs(profile: Path, eid: str, entry) -> None:
    (profile / "Secure Preferences").write_text(
        json.dumps({"extensions": {"settings": {eid: entry}}}), encoding="utf-8"
    )


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Chỉ cần đúng hai thứ mà `_is_extension_client` nhìn vào."""

    def __init__(self, origin="", host="127.0.0.1"):
        self.headers = {}
        if origin:
            self.headers["origin"] = origin
        self.client = _FakeClient(host)


class TestChromePaths(unittest.TestCase):
    def test_norm_case_and_separators(self):
        self.assertEqual(_norm(r"c:\foo\bar"), _norm(r"C:/FOO\bar\\"))

    def test_parse_user_data_dir_quoted(self):
        cmd = '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --user-data-dir="D:\\New folder\\Profile 1" --app=https://gemini.google.com/app'
        dirs = _parse_user_data_dirs(cmd)
        self.assertIn(_norm(r"D:\New folder\Profile 1"), dirs)

    def test_parse_user_data_dir_unquoted(self):
        cmd = "chrome --user-data-dir=C:\\Chrome\\ProfileX --no-first-run"
        dirs = _parse_user_data_dirs(cmd)
        self.assertIn(_norm(r"C:\Chrome\ProfileX"), dirs)

    def test_parse_user_data_dir_unquoted_with_spaces(self):
        # Đúng dạng Chrome 152 gửi: không ngoặc nháy, path mặc định có khoảng trắng.
        cmd = (
            '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --type=crashpad-handler '
            '"--user-data-dir=C:\\Users\\Dell\\AppData\\Local\\Google\\Chrome\\User Data" '
            "/prefetch:4 --monitor-self-annotation=ptype=crashpad-handler"
        )
        dirs = _parse_user_data_dirs(cmd)
        self.assertEqual(dirs, {_norm(r"C:\Users\Dell\AppData\Local\Google\Chrome\User Data")})

    def test_parse_user_data_dir_unquoted_stops_at_flag(self):
        cmd = 'chrome --user-data-dir=C:\\New folder\\Clone /prefetch:4 --app=https://x.test/'
        dirs = _parse_user_data_dirs(cmd)
        self.assertEqual(dirs, {_norm(r"C:\New folder\Clone")})

    def test_parse_user_data_dir_ignores_other_dirs(self):
        cmd = 'chrome --user-data-dir=C:\\A --other=C:\\B'
        self.assertEqual(_parse_user_data_dirs(cmd), {_norm(r"C:\A")})

    def test_build_command_offscreen(self):
        cmd = build_chrome_command(
            r"C:\chrome.exe", r"C:\Profiles\P1", hidden="offscreen", url="https://gemini.google.com/app"
        )
        self.assertIn(r"--user-data-dir=C:\Profiles\P1", cmd)
        self.assertIn("--window-position=-32000,-32000", cmd)
        self.assertNotIn("--headless=new", cmd)
        self.assertEqual(cmd[-1], "https://gemini.google.com/app")

    def test_build_command_headless(self):
        cmd = build_chrome_command(r"C:\chrome.exe", r"C:\Profiles\P1", hidden="headless")
        self.assertIn("--headless=new", cmd)
        self.assertNotIn("--window-position=-32000,-32000", cmd)

    def test_build_command_hidden_offscreen_and_anti_throttle(self):
        # `hidden`: off-screen nhưng KHÔNG SW_HIDE, kèm cờ chống throttle để
        # renderer vẫn chạy (Gemini mới dùng được tool tạo file).
        cmd = build_chrome_command(r"C:\chrome.exe", r"C:\Profiles\P1", hidden="hidden")
        self.assertIn("--window-position=-32000,-32000", cmd)
        self.assertNotIn("--headless=new", cmd)
        self.assertIn("--disable-backgrounding-occluded-windows", cmd)
        self.assertIn("--disable-renderer-backgrounding", cmd)
        self.assertIn("--disable-background-timer-throttling", cmd)
        self.assertIn(
            "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling", cmd
        )

    def test_build_command_visible_keeps_throttle_off_flags(self):
        cmd = build_chrome_command(r"C:\chrome.exe", r"C:\Profiles\P1", hidden="")
        self.assertNotIn("--window-position=-32000,-32000", cmd)
        self.assertNotIn("--headless=new", cmd)

    def test_build_command_profile_directory_and_extension(self):
        cmd = build_chrome_command(
            r"C:\chrome.exe", r"C:\Clone", hidden="offscreen",
            ext_dir=str(EXT_DIR), profile_directory="Profile 28",
        )
        self.assertIn("--profile-directory=Profile 28", cmd)
        self.assertTrue(any(a.startswith("--load-extension=") for a in cmd))

    def test_build_command_skips_load_extension_when_dir_missing(self):
        cmd = build_chrome_command(r"C:\chrome.exe", r"C:\Clone", ext_dir=r"C:\nope")
        self.assertFalse(any(a.startswith("--load-extension=") for a in cmd))


class TestExtensionId(unittest.TestCase):
    def test_id_is_32_lowercase_letters(self):
        eid = extension_id_from_path(str(BASE_DIR / "gemini-cookie-sync-extension"))
        self.assertEqual(len(eid), 32)
        # Chromium map nibble sang 'a'..'p' - KHÔNG phải hex 0-9a-f.
        self.assertTrue(all("a" <= c <= "p" for c in eid), eid)

    def test_id_depends_on_path(self):
        self.assertNotEqual(extension_id_from_path(r"C:\foo\bar"), extension_id_from_path(r"C:\foo\baz"))

    def test_id_matches_chromium_reference_vectors(self):
        # Tham chiếu: id mà Chrome 152 thực sự ghi cho 2 đường dẫn này.
        self.assertEqual(
            extension_id_from_path(r"D:\New folder\Lumi-Agent\Gemini-API\gemini-cookie-sync-extension"),
            "kkhhnbekcmeinhdjipacpbahalagbnim",
        )
        self.assertEqual(
            extension_id_from_path(r"D:\New folder\Gemini-API\gemini-cookie-sync-extension"),
            "mopfgdaigbnegpccjlhgenldpfljmgeh",
        )

    def test_installed_negative_and_positive_from_store_dir(self):
        # Tham số là thư mục profile (User Data\\Default), nên Extensions nằm
        # ngay bên trong nó.
        profile = Path(tempfile.mkdtemp(prefix="ext-profile-"))
        self.assertFalse(extension_installed_in(str(profile)))
        eid = extension_id_from_path(str(EXT_DIR))
        ver = profile / "Extensions" / eid / "1.2"
        ver.mkdir(parents=True)
        (ver / "manifest.json").write_text("{}", encoding="utf-8")
        self.assertTrue(extension_installed_in(str(profile)))

    def test_not_installed_when_only_another_extension_present(self):
        profile = Path(tempfile.mkdtemp(prefix="ext-profile-"))
        other = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        ver = profile / "Extensions" / other / "1.0"
        ver.mkdir(parents=True)
        (ver / "manifest.json").write_text("{}", encoding="utf-8")
        self.assertFalse(extension_installed_in(str(profile)))


class TestExtensionRegistration(unittest.TestCase):
    """Chrome prune entry của extension unpacked trên user-data-dir bị copy, nên
    'còn ID trong Secure Preferences' KHÔNG đồng nghĩa 'extension chạy được'."""

    def setUp(self):
        self.profile = Path(tempfile.mkdtemp(prefix="ext-reg-"))
        self.eid = extension_id_from_path(str(EXT_DIR))

    def test_unpacked_with_existing_path_is_usable(self):
        _write_secure_prefs(self.profile, self.eid, {"path": str(EXT_DIR), "state": 1})
        info = extension_registration(str(self.profile))
        self.assertTrue(info["usable"])
        self.assertTrue(info["entry"])
        self.assertTrue(extension_installed_in(str(self.profile)))

    def test_pruned_entry_is_not_usable(self):
        # Đúng trạng thái Chrome tạo ra: còn ID, mất `path`.
        _write_secure_prefs(self.profile, self.eid, {"state": None, "location": None})
        info = extension_registration(str(self.profile))
        self.assertTrue(info["entry"])
        self.assertFalse(info["usable"])
        self.assertIn("prune", info["reason"])
        self.assertFalse(extension_installed_in(str(self.profile)))

    def test_entry_pointing_at_missing_dir_is_not_usable(self):
        _write_secure_prefs(self.profile, self.eid, {"path": r"C:\definitely\gone"})
        info = extension_registration(str(self.profile))
        self.assertFalse(info["usable"])
        self.assertIn("không tồn tại", info["reason"])

    def test_no_entry_at_all(self):
        info = extension_registration(str(self.profile))
        self.assertFalse(info["entry"])
        self.assertFalse(info["usable"])

    def test_store_install_wins_over_missing_entry(self):
        ver = self.profile / "Extensions" / self.eid / "1.3"
        ver.mkdir(parents=True)
        (ver / "manifest.json").write_text("{}", encoding="utf-8")
        _write_secure_prefs(self.profile, self.eid, {})
        info = extension_registration(str(self.profile))
        self.assertTrue(info["in_store"])
        self.assertTrue(info["usable"])

    def test_missing_extension_dir_is_reported(self):
        info = extension_registration(str(self.profile), ext_dir=r"C:\no\such\ext")
        self.assertFalse(info["usable"])
        self.assertIn("Không thấy thư mục extension", info["reason"])


class TestAgentProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-udd-"))
        self._old = os.environ.get("GEMINI_AGENT_USER_DATA_DIR")
        os.environ["GEMINI_AGENT_USER_DATA_DIR"] = str(self.tmp)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("GEMINI_AGENT_USER_DATA_DIR", None)
        else:
            os.environ["GEMINI_AGENT_USER_DATA_DIR"] = self._old

    def test_default_agent_profile_name(self):
        agent = resolve_agent_profile({})
        self.assertEqual(agent.name, DEFAULT_AGENT_PROFILE)
        self.assertEqual(agent.user_data_dir, str(self.tmp))
        self.assertEqual(agent.label, "agent")

    def test_agent_profile_honours_config(self):
        agent = resolve_agent_profile({"profile_directory": "Bot", "user_data_dir": r"D:\udd"})
        self.assertEqual(agent.name, "Bot")
        self.assertEqual(agent.user_data_dir, r"D:\udd")

    def test_agent_user_data_is_not_chrome_default(self):
        # Ràng buộc cốt lõi: agent KHÔNG được dùng chung User Data với Chrome
        # cá nhân, nếu không sẽ tranh instance và giấu nhầm cửa sổ người dùng.
        import chrome_launcher

        os.environ.pop("GEMINI_AGENT_USER_DATA_DIR", None)
        self.assertNotEqual(
            _norm(str(chrome_launcher.agent_user_data_dir())),
            _norm(str(chrome_launcher.user_data_root())),
        )

    def test_default_mode_is_agent_and_never_clones(self):
        target = prepare_work_profile({})
        self.assertIsNotNone(target)
        self.assertEqual(target.label, "agent")
        self.assertEqual(_norm(target.user_data_dir), _norm(str(self.tmp)))

    def test_clone_mode_is_rejected(self):
        self.assertIsNone(prepare_work_profile({"mode": "clone"}))


class TestProfileScopedPids(unittest.TestCase):
    def test_parse_profile_directory(self):
        self.assertEqual(
            _parse_profile_directory(r"chrome.exe --profile-directory=Profile 28 --no-first-run"),
            "Profile 28",
        )
        self.assertEqual(
            _parse_profile_directory(r'chrome.exe --profile-directory="Lumi Agent" --x'),
            "Lumi Agent",
        )
        self.assertEqual(
            _parse_profile_directory(r"chrome.exe --profile-directory=Lumi-Agent /prefetch:4"),
            "Lumi-Agent",
        )
        self.assertIsNone(_parse_profile_directory("chrome.exe --no-first-run"))

    def test_pids_filtered_by_profile_name(self):
        import chrome_launcher

        udd = r"C:\Users\D\AppData\Local\Google\Chrome\User Data"
        fake = [
            (10, f'chrome.exe --user-data-dir="{udd}" --profile-directory=Lumi-Agent'),
            (11, f'chrome.exe --user-data-dir="{udd}" --profile-directory="Profile 28"'),
            (12, f'chrome.exe --user-data-dir="{udd}"'),  # Chrome mở tay
            (13, r"chrome.exe --user-data-dir=C:\Other"),
        ]
        orig = chrome_launcher._chrome_processes
        chrome_launcher._chrome_processes = lambda: fake
        try:
            self.assertEqual(profile_running_pids(udd), [10, 11, 12])
            self.assertEqual(profile_running_pids(udd, "Lumi-Agent"), [10])
            self.assertEqual(profile_running_pids(udd, "Profile 28"), [11])
            self.assertEqual(profile_running_pids(udd, "Khong Ton Tai"), [])
        finally:
            chrome_launcher._chrome_processes = orig


class TestSingleInstanceAwareness(unittest.TestCase):
    """Chrome chỉ cho 1 instance/user-data-dir: launch mới sẽ bàn giao cho instance
    cũ nên không process nào mang cờ của ta. keep_alive phải dò browser process."""

    def setUp(self):
        import chrome_launcher

        self.mod = chrome_launcher
        self.udd = r"C:\Users\D\AppData\Local\Google\Chrome\User Data"
        self._old = chrome_launcher.user_data_root
        chrome_launcher.user_data_root = lambda: Path(self.udd)
        self._old_procs = chrome_launcher._chrome_processes

    def tearDown(self):
        self.mod.user_data_root = self._old
        self.mod._chrome_processes = self._old_procs

    def _set(self, procs):
        self.mod._chrome_processes = lambda: procs

    def test_manual_chrome_counts_as_owning_default_user_data(self):
        # Chrome mở tay: KHÔNG có --user-data-dir nào trong command line.
        self._set([(99, "chrome.exe --no-first-run")])
        self.assertTrue(chrome_instance_alive(self.udd))
        self.assertEqual(browser_pids(self.udd), [99])

    def test_child_processes_do_not_count(self):
        self._set([(99, f'chrome.exe --user-data-dir="{self.udd}" --type=renderer --x')])
        self.assertFalse(chrome_instance_alive(self.udd))
        self.assertEqual(browser_pids(self.udd), [])

    def test_explicit_user_data_dir_counts(self):
        self._set([(7, f'chrome.exe --user-data-dir="{self.udd}" --profile-directory=Lumi-Agent')])
        self.assertTrue(chrome_instance_alive(self.udd))
        self.assertEqual(browser_pids(self.udd), [7])

    def test_other_user_data_dir_does_not_count(self):
        self._set([(7, 'chrome.exe --user-data-dir="C:\\Elsewhere"')])
        self.assertFalse(chrome_instance_alive(self.udd))

    def test_no_chrome_at_all(self):
        self._set([])
        self.assertFalse(chrome_instance_alive(self.udd))


class TestProfileDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chrome-udd-"))
        self._old_udd = os.environ.get("CHROME_USER_DATA_DIR")

    def tearDown(self):
        if self._old_udd is None:
            os.environ.pop("CHROME_USER_DATA_DIR", None)
        else:
            os.environ["CHROME_USER_DATA_DIR"] = self._old_udd

    def _fake_udd(self, secure_prefs: bool):
        (self.tmp / "Local State").write_text("{}", encoding="utf-8")
        eid = extension_id_from_path(str(EXT_DIR))
        for label in ("Default", "Profile 1"):
            prof = self.tmp / label
            prof.mkdir(parents=True)
            entry = {
                "extensions": {
                    "settings": {
                        eid: {"path": str(EXT_DIR), "location": 4, "state": 1}
                    }
                }
            }
            name = "Secure Preferences" if secure_prefs else "Preferences"
            (prof / name).write_text(json.dumps(entry), encoding="utf-8")
        (self.tmp / "Guest Profile").mkdir()
        os.environ["CHROME_USER_DATA_DIR"] = str(self.tmp)

    def test_discovers_unpacked_via_secure_preferences(self):
        self._fake_udd(secure_prefs=True)
        self.assertEqual([p.name for p in discover_profiles()], ["Default", "Profile 1"])

    def test_discovers_unpacked_via_preferences(self):
        self._fake_udd(secure_prefs=False)
        self.assertEqual([p.name for p in discover_profiles()], ["Default", "Profile 1"])

    def test_list_profiles_skips_guest(self):
        self._fake_udd(secure_prefs=True)
        self.assertEqual([p.name for p in list_profiles()], ["Default", "Profile 1"])

    def test_find_profiles_with_extension_returns_profile_dirs(self):
        self._fake_udd(secure_prefs=True)
        labels = [m[0] for m in find_profiles_with_extension()]
        self.assertEqual(labels, ["Default", "Profile 1"])
        self.assertEqual(find_profiles_with_extension()[0][1], str(self.tmp / "Default"))

    def test_chrome_profile_paths(self):
        p = ChromeProfile(r"C:\UDD", "Profile 28", "Profile 28")
        self.assertEqual(p.profile_dir, r"C:\UDD\Profile 28")
        self.assertIn("Profile 28", p.describe())

    def test_resolve_template_auto_picks_first(self):
        self._fake_udd(secure_prefs=True)
        tpl = resolve_template_profile({})
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl.name, "Default")
        self.assertEqual(tpl.user_data_dir, str(self.tmp))

    def test_resolve_template_rejects_missing_explicit(self):
        self._fake_udd(secure_prefs=True)
        self.assertIsNone(resolve_template_profile({"source_profile": r"C:\khong\ton\ tai"}))

    def test_resolve_template_accepts_profile_dir_under_udd(self):
        self._fake_udd(secure_prefs=True)
        tpl = resolve_template_profile({"source_profile": str(self.tmp / "Profile 1")})
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl.name, "Profile 1")

    def test_resolve_template_accepts_whole_user_data_dir(self):
        self._fake_udd(secure_prefs=True)
        tpl = resolve_template_profile({"source_profile": str(self.tmp)})
        self.assertIsNotNone(tpl)
        self.assertEqual(tpl.name, "Default")

    def test_resolve_profile_legacy_signature(self):
        self._fake_udd(secure_prefs=True)
        path, label = resolve_profile("")
        self.assertEqual(label, "Default")
        self.assertTrue(os.path.isdir(path))


class TestWorkDir(unittest.TestCase):
    def test_default_is_inside_project(self):
        self.assertEqual(resolve_work_dir({}), BASE_DIR / DEFAULT_WORK_DIR)

    def test_relative_is_resolved_against_project(self):
        self.assertEqual(resolve_work_dir({"work_dir": "copy1"}), BASE_DIR / "copy1")

    def test_absolute_is_kept(self):
        self.assertEqual(resolve_work_dir({"work_dir": r"D:\somewhere"}), Path(r"D:\somewhere"))


class TestCloneArgs(unittest.TestCase):
    def test_robocopy_copies_profile_tree_and_excludes_caches(self):
        args = _robocopy_args(r"C:\src\Profile 28", r"C:\dst\Profile 28", True)
        self.assertEqual(args[0], "robocopy")
        self.assertEqual(args[1], r"C:\src\Profile 28")
        self.assertEqual(args[2], r"C:\dst\Profile 28")
        self.assertIn("/E", args)
        self.assertIn("/MIR", args)
        self.assertIn("Code Cache", args)
        self.assertIn("Service Worker\\CacheStorage", args)
        self.assertIn("Singleton*", args)

    def test_robocopy_without_mirror(self):
        self.assertNotIn("/MIR", _robocopy_args(r"C:\a", r"C:\b", False))

    def test_robocopy_optional_file_spec(self):
        self.assertIn("Local State", _robocopy_args(r"C:\a", r"C:\b", False, ["Local State"]))

    def test_clone_same_dir_is_noop(self):
        src = ChromeProfile(r"C:\Same", "Default", "Default")
        self.assertTrue(clone_profile(src, r"C:\Same"))

    def test_clone_missing_source_fails(self):
        src = ChromeProfile(r"C:\NoSuchUdd", "Default", "Default")
        self.assertFalse(clone_profile(src, str(Path(tempfile.mkdtemp(prefix="clone-"))), retries=1))


class TestInUseDetection(unittest.TestCase):
    def _profile(self, with_db=True):
        prof = Path(tempfile.mkdtemp(prefix="inuse-"))
        if with_db:
            db = prof / "Network"
            db.mkdir()
            (db / "Cookies").write_bytes(b"SQLite format 3\x00")
        return prof

    def test_unlocked_profile_is_not_in_use(self):
        self.assertFalse(chrome_data_in_use(str(self._profile())))

    def test_missing_db_means_not_in_use(self):
        self.assertFalse(chrome_data_in_use(str(self._profile(with_db=False))))

    def test_probe_leaves_no_residue(self):
        prof = self._profile()
        chrome_data_in_use(str(prof))
        self.assertEqual(sorted(p.name for p in (prof / "Network").iterdir()), ["Cookies"])


class TestCloneState(unittest.TestCase):
    def test_state_flags_essentials(self):
        prof = Path(tempfile.mkdtemp(prefix="state-"))
        state = _clone_state(str(prof))
        self.assertFalse(any(state.values()))
        (prof / "Preferences").write_text("{}", encoding="utf-8")
        (prof / "Secure Preferences").write_text("{}", encoding="utf-8")
        (prof / "Network").mkdir()
        (prof / "Network" / "Cookies").write_bytes(b"")
        self.assertTrue(all(_clone_state(str(prof)).values()))


class TestBootstrapOriginGate(unittest.TestCase):
    """`_is_extension_client` không được dựa vào `Origin`, vì Chrome không gửi
    header đó cho `fetch()` GET CORS-simple từ extension service worker."""

    def _call(self, origin, host="127.0.0.1"):
        return _is_extension_client(
            _FakeRequest(origin=origin, host=host), origin
        )

    def test_extension_origin_accepted(self):
        self.assertTrue(self._call("chrome-extension://kkhhnbekcmeinhdjipacpbalhagbnim"))

    def test_missing_origin_from_loopback_accepted(self):
        # Trường hợp thật: Chrome bỏ header Origin trên GET CORS-simple.
        self.assertTrue(self._call(""))

    def test_web_page_origin_rejected(self):
        # Hàng rào thật sự: trang web trong Chrome gọi vào localhost để lấy key.
        self.assertFalse(self._call("https://gemini.google.com"))
        self.assertFalse(self._call("https://evil.example"))
        self.assertFalse(self._call("null"))

    def test_non_loopback_without_origin_rejected(self):
        self.assertFalse(self._call("", host="192.168.1.50"))


class TestCloseAgentChrome(unittest.TestCase):
    """`close_agent_chrome` phải đóng lịch sự trước.

    Kill cưỡng bức làm Chrome không ghi `Secure Preferences`, mà đó là nơi lưu
    extension unpacked — kill cứng thì extension biến mất. Nên `/F` chỉ được dùng
    sau khi đã thử đóng lịch sự và thất bại.
    """

    def setUp(self):
        self.calls = []
        self.pid_script = [[1234], [1234], []]  # 2 lần đọc còn sống, rồi đã thoát
        # Phải patch `chrome_launcher.browser_pids`, không phải bản import ở đây:
        # close_agent_chrome gọi tên đó trong module của nó.
        self._real_run, self._real_sleep = subprocess.run, time.sleep
        self._real_pids = chrome_launcher_mod.browser_pids
        subprocess.run = self._fake_run
        time.sleep = lambda _s: None
        chrome_launcher_mod.browser_pids = (
            lambda _udd: self.pid_script.pop(0) if self.pid_script else []
        )

    def tearDown(self):
        subprocess.run = self._real_run
        time.sleep = self._real_sleep
        chrome_launcher_mod.browser_pids = self._real_pids

    def _fake_run(self, cmd, **_kw):
        self.calls.append(cmd)
        return None

    def test_graceful_close_does_not_force(self):
        self.assertTrue(close_agent_chrome("/udd", timeout=1))
        self.assertEqual(self.calls, [["taskkill", "/PID", "1234"]])
        self.assertNotIn("/F", self.calls[0])

    def test_escalates_to_force_only_after_timeout(self):
        # Không bao giờ chịu thoát -> phải có bước /F
        chrome_launcher_mod.browser_pids = lambda _u: [1234]
        self.assertFalse(close_agent_chrome("/udd", timeout=0))
        self.assertIn(["taskkill", "/F", "/PID", "1234"], self.calls)

    def test_no_pids_is_noop(self):
        chrome_launcher_mod.browser_pids = lambda _u: []
        self.assertTrue(close_agent_chrome("/udd", timeout=1))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
