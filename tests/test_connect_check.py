"""SnowLuma <-> 糖糖 接线自检（tools/检查连接.py）的回归测试。

## 为什么断言 next_step 里的具体数值

这个工具存在的**全部意义**是把静默失败变成「指名道姓的下一步」。所以每个
FAIL/WARN 分支的断言都不止断言状态码，而是断言文案里带着可照做的数值
（`ws://127.0.0.1:3001`、端口 3000、`${SNOWLUMA_TOKEN}` 该去哪儿填）——
只断言 status 的话，文案退化成「连接失败」这种废话也照样绿。

## 端口探测必须可注入

`check_all(..., port_probe=...)` 是硬要求：不注入就会去连本机真实的 3000/5099/3001，
测试结果取决于**跑测试时本机开没开 SnowLuma**——随机红，比不写还糟。
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parent.parent
TOOL = BASE / "tools" / "检查连接.py"

# 假号（8 位，故意低于发布脱敏扫描器的 9~11 位 QQ 候选阈值）
QQ = "10001000"
OTHER_QQ = "10002000"


@pytest.fixture(scope="module")
def chk():
    spec = importlib.util.spec_from_file_location("connect_check_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════
# 假项目根：造一份 SnowLuma 安装 + config.yaml + .env
# ═══════════════════════════════════════════════════════

def _make_snowluma(root, version="1.14.9", *, webui_port=5099, launcher=True,
                   runtime=True, onebot=None, node=True):
    d = root / "SnowLuma" / f"SnowLuma-v{version}-win-x64"
    (d / "config").mkdir(parents=True, exist_ok=True)
    if launcher:
        (d / "launcher.bat").write_text("@echo off\r\n", encoding="utf-8")
    # 真包自带 node.exe（launcher.bat 第一行就是 `node .\check-node-version.cjs`）。
    # **-lite 版不带** —— 那条单独有测试，这里默认按真包造。
    if node:
        (d / "node.exe").write_bytes(b"MZ")
    if runtime:
        (d / "config" / "runtime.json").write_text(
            json.dumps({"webuiPort": webui_port}), encoding="utf-8")
    for qq, doc in (onebot or {}).items():
        (d / "config" / f"onebot_{qq}.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return d


def _doc(http_port=3000, http_token="", ws_url="ws://127.0.0.1:3001",
         ws_token="", ws_servers=None):
    """造一份 onebot_<QQ>.json。端口/url 传 None = 那一项干脆不建。"""
    nets = {"httpServers": [], "httpClients": [],
            "wsServers": list(ws_servers or []), "wsClients": []}
    if http_port is not None:
        nets["httpServers"].append({"name": "http-default", "accessToken": http_token,
                                    "host": "0.0.0.0", "port": http_port, "path": "/"})
    if ws_url is not None:
        nets["wsClients"].append({"name": "wsclient-1", "accessToken": ws_token,
                                  "url": ws_url, "role": "Universal"})
    return {"mode": "snapshot", "networks": nets}


def _write_config(root, qq=QQ, token="${SNOWLUMA_TOKEN}",
                  http_url="http://127.0.0.1:3000", **napcat_extra):
    napcat = {"access_token": token, "http_url": http_url}
    napcat.update(napcat_extra)
    (root / "config.yaml").write_text(
        yaml.safe_dump({"bot": {"qq_id": qq}, "napcat": napcat}, allow_unicode=True),
        encoding="utf-8")


def _write_env(root, **pairs):
    (root / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in pairs.items()) + "\n", encoding="utf-8")


def _probe(*open_ports):
    ports = set(open_ports)
    return lambda host, port: port in ports


def _run(chk, root, *open_ports):
    # environ={} 是刻意的：不让跑测试那台机器真实的环境变量漏进来
    return chk.check_all(root, port_probe=_probe(*open_ports), environ={})


def _get(results, key):
    return next(r for r in results if r.key == key)


def _checks(results, chk):
    return [r for r in results if r.key != chk.KEY_SUMMARY]


def _summary(results):
    return results[-1]


@pytest.fixture
def fake_ports(chk, monkeypatch):
    """给 main() 换掉真实探针——main 不能注入，只能换模块里的默认实现。

    不加这个的话，「哪些端口开着」取决于跑测试时本机开没开 SnowLuma。
    """

    def _set(*open_ports):
        ports = set(open_ports)
        monkeypatch.setattr(chk, "port_open", lambda host, port: port in ports)
        return ports

    return _set


# 一份「全都对」的假根：装好、登录好、反向 WS 3001、HTTP 3000、token 三边一致
@pytest.fixture
def good_root(tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_token="tok-abcdefgh",
                                              ws_token="tok-abcdefgh")})
    _write_config(tmp_path)
    _write_env(tmp_path, SNOWLUMA_TOKEN="tok-abcdefgh")
    return tmp_path


# ═══════════════════════════════════════════════════════
# 1. SnowLuma 装了吗
# ═══════════════════════════════════════════════════════

def test_missing_snowluma_points_at_release_page(chk, tmp_path):
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_INSTALLED)
    assert r.status is chk.Status.FAIL
    assert chk.RELEASE_URL in r.next_step
    # 解压落点要具体到用户能照做
    assert str(tmp_path / "SnowLuma") in r.next_step


def test_picks_highest_version_dir(chk, tmp_path):
    _make_snowluma(tmp_path, version="1.13.0")
    _make_snowluma(tmp_path, version="1.14.9")
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_INSTALLED)
    assert r.status is chk.Status.OK
    assert "SnowLuma-v1.14.9-win-x64" in r.detail


def test_incomplete_dir_does_not_shadow_a_real_install(chk, tmp_path):
    """仓库里真躺着一个只剩 logs/ 的 SnowLuma-v1.11.4-win-x64-lite。

    版本更高但缺 launcher.bat 的残缺目录，不该顶掉那份能用的安装。
    """
    _make_snowluma(tmp_path, version="1.99.0", launcher=False, runtime=False)
    _make_snowluma(tmp_path, version="1.14.9")
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_INSTALLED)
    assert r.status is chk.Status.OK
    assert "SnowLuma-v1.14.9-win-x64" in r.detail


def test_incomplete_dir_alone_is_warned_with_fix(chk, tmp_path):
    _make_snowluma(tmp_path, version="1.14.9", launcher=False)
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_INSTALLED)
    assert r.status is chk.Status.WARN
    assert "launcher.bat" in r.next_step


# ═══════════════════════════════════════════════════════
# 2. SnowLuma 在跑吗
# ═══════════════════════════════════════════════════════

def test_not_running_tells_you_which_button(chk, good_root):
    r = _get(_run(chk, good_root), chk.KEY_RUNNING)
    assert r.status is chk.Status.FAIL
    assert "启动 SnowLuma" in r.next_step
    # 手改路径也要给全：launcher.bat 的实际位置
    assert "launcher.bat" in r.next_step


def test_running_when_only_webui_port_answers(chk, good_root):
    r = _get(_run(chk, good_root, 5099), chk.KEY_RUNNING)
    assert r.status is chk.Status.OK
    assert "5099" in r.detail


def test_running_when_only_http_port_answers(chk, good_root):
    r = _get(_run(chk, good_root, 3000), chk.KEY_RUNNING)
    assert r.status is chk.Status.OK
    assert "3000" in r.detail


def test_webui_port_comes_from_runtime_json(chk, tmp_path):
    _make_snowluma(tmp_path, webui_port=7777, onebot={QQ: _doc()})
    _write_config(tmp_path)
    # 只有非默认端口开着，才证明端口真是从 runtime.json 读出来的
    r = _get(_run(chk, tmp_path, 7777), chk.KEY_RUNNING)
    assert r.status is chk.Status.OK
    assert "7777" in r.detail


# ═══════════════════════════════════════════════════════
# 3. OneBot 配置 / 两边 QQ 号
# ═══════════════════════════════════════════════════════

def test_no_onebot_file_means_qq_not_logged_in(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={})
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_ONEBOT)
    assert r.status is chk.Status.FAIL
    # 断言"说了怎么让 QQ 登录上"，而不是某个具体动词——
    # 原话写的是"扫码登录"，而 SnowLuma **没有二维码登录**：它注入到已运行的 QQ 进程里。
    assert "QQ 客户端" in r.next_step and QQ in r.next_step


def test_wrong_qq_prints_both_numbers(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={OTHER_QQ: _doc()})
    _write_config(tmp_path, qq=QQ)
    r = _get(_run(chk, tmp_path), chk.KEY_ONEBOT)
    assert r.status is chk.Status.FAIL
    assert QQ in r.detail and OTHER_QQ in r.detail
    assert QQ in r.next_step or OTHER_QQ in r.next_step


def test_empty_bot_qq_tells_you_where_to_fill(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc()})
    _write_config(tmp_path, qq="")
    r = _get(_run(chk, tmp_path), chk.KEY_ONEBOT)
    assert r.status is chk.Status.FAIL
    assert "bot.qq_id" in r.next_step


def test_unreadable_config_yaml_is_reported_not_raised(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc()})  # 不写 config.yaml
    results = _run(chk, tmp_path)  # 不抛异常才算过
    r = _get(results, chk.KEY_ONEBOT)
    assert r.status is chk.Status.FAIL
    assert "config.yaml" in r.next_step


def test_downstream_checks_skip_instead_of_failing_when_upstream_broken(chk, tmp_path):
    """上游没就绪时后续项是 [ ] 而不是一堆 [×]——结论要指向真正卡住的那一步。"""
    _make_snowluma(tmp_path, onebot={})
    _write_config(tmp_path)
    # SnowLuma 算在跑，免得第 2 步抢在前面当「卡住的那一步」（那个另有用例覆盖）
    results = _run(chk, tmp_path, 5099)
    for key in (chk.KEY_WS_CLIENT, chk.KEY_WS_SERVER, chk.KEY_HTTP_SERVER,
                chk.KEY_TOKEN):
        skipped = _get(results, key)
        assert skipped.status is chk.Status.SKIP, key
        assert "第 3 步" in skipped.next_step, key
    assert "卡在第 3 步" in _summary(results).detail


# ═══════════════════════════════════════════════════════
# 4. 反向 WS 客户端
# ═══════════════════════════════════════════════════════

def test_ws_client_missing_gives_the_exact_url(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc(ws_url=None)})
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_WS_CLIENT)
    assert r.status is chk.Status.FAIL
    assert "ws://127.0.0.1:3001" in r.next_step
    assert "WS 客户端" in r.next_step


def test_ws_client_left_at_default_8080_is_called_out(chk, tmp_path):
    """新建 wsClient 的默认 url 是 ws://127.0.0.1:8080/ws——这是最常见的新手坑。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(ws_url="ws://127.0.0.1:8080/ws")})
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_WS_CLIENT)
    assert r.status is chk.Status.FAIL
    assert "8080" in r.detail and "默认值" in r.detail
    assert "ws://127.0.0.1:3001" in r.next_step


@pytest.mark.parametrize("url", ["ws://127.0.0.1:9999", "ws://127.0.0.1/ws",
                                 "ws://example.com:3000/ws"])
def test_ws_client_wrong_target_lists_both_sides(chk, tmp_path, url):
    _make_snowluma(tmp_path, onebot={QQ: _doc(ws_url=url)})
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_WS_CLIENT)
    assert r.status is chk.Status.FAIL
    assert url in r.detail                      # 现在指的是哪儿
    assert "ws://127.0.0.1:3001" in r.next_step  # 该改成哪儿


def test_ws_client_localhost_spelling_is_accepted(chk, tmp_path):
    """配 localhost 与 127.0.0.1 是同一件事，不该判 FAIL。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(ws_url="ws://localhost:3001/")})
    _write_config(tmp_path)
    assert _get(_run(chk, tmp_path), chk.KEY_WS_CLIENT).status is chk.Status.OK


# ═══════════════════════════════════════════════════════
# 5. 误建的「WS 服务端」抢端口
# ═══════════════════════════════════════════════════════

def test_ws_server_stealing_3001_is_its_own_failure(chk, tmp_path):
    """新建「WS 服务端」的默认就是 127.0.0.1:3001——选错标签页会先把端口占住。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(
        ws_servers=[{"name": "wsserver-1", "host": "127.0.0.1", "port": 3001}])})
    _write_config(tmp_path)
    results = _run(chk, tmp_path)
    assert _get(results, chk.KEY_WS_CLIENT).status is chk.Status.OK  # 客户端那条是对的
    r = _get(results, chk.KEY_WS_SERVER)
    assert r.status is chk.Status.FAIL
    assert "3001" in r.detail and "服务端" in r.next_step
    assert _summary(results).status is chk.Status.FAIL


def test_ws_server_on_another_port_is_not_a_conflict(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc(
        ws_servers=[{"name": "wsserver-1", "host": "127.0.0.1", "port": 8080}])})
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_WS_SERVER)
    assert r.status is chk.Status.OK
    assert "8080" in r.detail


# ═══════════════════════════════════════════════════════
# 6. HTTP 服务器
# ═══════════════════════════════════════════════════════

def test_http_server_wrong_port_says_where_to_change(chk, tmp_path):
    """真实样本 onebot_1743800785.json 就是端口 3010 这种近乎默认的配置。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_port=3010)})
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_HTTP_SERVER)
    assert r.status is chk.Status.FAIL
    assert "3010" in r.detail
    assert "3000" in r.next_step


def test_http_server_missing_gives_new_api_steps(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_port=None)})
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_HTTP_SERVER)
    assert r.status is chk.Status.FAIL
    assert "HTTP API" in r.next_step and "3000" in r.next_step


def test_http_url_in_config_is_the_reference(chk, tmp_path):
    """config.yaml 改了口径，判据跟着走——不能写死 3000。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_port=3010)})
    _write_config(tmp_path, http_url="http://127.0.0.1:3010")
    r = _get(_run(chk, tmp_path), chk.KEY_HTTP_SERVER)
    assert r.status is chk.Status.OK
    assert "3010" in r.detail


# ═══════════════════════════════════════════════════════
# 7. 授权 Token 三边
# ═══════════════════════════════════════════════════════

def test_token_all_empty_is_ok(chk, tmp_path):
    """两边都留空是允许的：SnowLuma 侧留空 = 全部放行。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_token="", ws_token="")})
    _write_config(tmp_path, token="")
    r = _get(_run(chk, tmp_path), chk.KEY_TOKEN)
    assert r.status is chk.Status.OK
    assert "留空" in r.detail


def test_token_mismatch_masks_the_sugar_side(chk, tmp_path):
    sugar_tok = "8bzJFAKEtoken~oW"
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_token="snowLumaSideToken01",
                                              ws_token="snowLumaSideToken01")})
    _write_config(tmp_path, token=sugar_tok)
    r = _get(_run(chk, tmp_path), chk.KEY_TOKEN)
    assert r.status is chk.Status.WARN
    # 打码：糖糖那串不能整串出现在输出里，但要能认出是同一串
    assert sugar_tok not in r.detail and sugar_tok not in r.next_step
    assert "8bzJ****oW" in r.detail  # 前 4 后 2，中间星号
    assert "授权 Token" in r.next_step
    # 这条工具的常识：不一致的表现是发不出去，不是连不上
    assert "401" in r.detail


def test_token_identical_is_ok(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_token="samesame1234",
                                              ws_token="samesame1234")})
    _write_config(tmp_path, token="samesame1234")
    r = _get(_run(chk, tmp_path), chk.KEY_TOKEN)
    assert r.status is chk.Status.OK


def test_unresolved_placeholder_is_a_fail_pointing_at_secret_manager(chk, tmp_path):
    """⚠ 环境变量缺失时 main.py 保留字面量——「看起来有值，其实是占位符」。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_token="snowLumaSideToken01",
                                              ws_token="snowLumaSideToken01")})
    _write_config(tmp_path, token="${SNOWLUMA_TOKEN}")
    _write_env(tmp_path, DEEPSEEK_KEY="whatever")  # .env 在，但没这个变量
    r = _get(_run(chk, tmp_path), chk.KEY_TOKEN)
    assert r.status is chk.Status.FAIL
    assert "SNOWLUMA_TOKEN" in r.next_step
    assert "密钥管理" in r.next_step


def test_placeholder_resolved_from_env_file_is_ok(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_token="resolved-token-123",
                                              ws_token="resolved-token-123")})
    _write_config(tmp_path, token="${SNOWLUMA_TOKEN}")
    _write_env(tmp_path, SNOWLUMA_TOKEN="resolved-token-123")
    assert _get(_run(chk, tmp_path), chk.KEY_TOKEN).status is chk.Status.OK


def test_placeholder_resolved_from_process_env_is_ok(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_token="from-environ-987",
                                              ws_token="from-environ-987")})
    _write_config(tmp_path, token="${SNOWLUMA_TOKEN}")
    results = chk.check_all(tmp_path, port_probe=_probe(),
                            environ={"SNOWLUMA_TOKEN": "from-environ-987"})
    assert _get(results, chk.KEY_TOKEN).status is chk.Status.OK


def test_token_only_in_ws_client_is_a_warn_not_an_ok(chk, tmp_path):
    """糖糖不校验入站 token，但 SnowLuma 留空 = 全部放行——能用，值得提一句。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_token="", ws_token="only-ws-1234")})
    _write_config(tmp_path, token="")
    r = _get(_run(chk, tmp_path), chk.KEY_TOKEN)
    assert r.status is chk.Status.WARN


# ═══════════════════════════════════════════════════════
# 8. 糖糖在监听吗 + 汇总结论
# ═══════════════════════════════════════════════════════

def test_sugar_not_listening_tells_you_to_start_it(chk, good_root):
    r = _get(_run(chk, good_root), chk.KEY_SUGAR_LISTEN)
    assert r.status is chk.Status.FAIL
    # 按钮名要和控制台**逐字**一致（`QPushButton(" 启动小糖糖")`）——
    # 原先写的是「启动糖糖」，它在界面上连子串都不是（S6）。
    assert "3001" in r.detail and "启动小糖糖" in r.next_step


def test_everything_good_is_all_ok(chk, good_root):
    results = _run(chk, good_root, 5099, 3000, 3001)
    for r in _checks(results, chk):
        assert r.status is chk.Status.OK, f"{r.key}: {r.detail}"
    assert _summary(results).status is chk.Status.OK


def test_summary_names_the_first_blocking_step(chk, tmp_path):
    _make_snowluma(tmp_path, onebot={QQ: _doc(ws_url="ws://127.0.0.1:8080/ws")})
    _write_config(tmp_path)
    results = _run(chk, tmp_path, 5099)
    first_bad = _get(results, chk.KEY_WS_CLIENT)
    s = _summary(results)
    assert s.status is chk.Status.FAIL
    assert "卡在第 4 步" in s.detail
    assert s.next_step == first_bad.next_step  # 结论直接抄那一步的下一步


def test_summary_prefers_the_earliest_step(chk, tmp_path):
    """第 2 步没起来时，结论先说第 2 步——修完再跑一次才会暴露后面的问题。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(ws_url="ws://127.0.0.1:8080/ws")})
    _write_config(tmp_path)
    s = _summary(_run(chk, tmp_path))
    assert s.status is chk.Status.FAIL
    assert "卡在第 2 步" in s.detail


def test_summary_step_number_is_the_printed_number(chk, tmp_path, capsys, fake_ports):
    """结论里的「第 N 步」必须和清单打印出来的序号对得上。"""
    _make_snowluma(tmp_path, onebot={QQ: _doc(http_port=None)})
    _write_config(tmp_path)
    fake_ports(5099)  # 第 2 步算过，好让卡住的那一步是 HTTP 服务器
    assert chk.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines()
             if ln.strip().endswith("有 HTTP 服务器在糖糖要的端口吗")]
    assert lines, out
    printed_no = lines[0].strip().split(".")[0]
    for glyph in ("[×]", "[!]", "[√]", "[ ]"):
        printed_no = printed_no.replace(glyph, "")
    printed_no = printed_no.strip()
    assert printed_no.isdigit(), lines[0]
    assert f"卡在第 {printed_no} 步" in out


# ═══════════════════════════════════════════════════════
# 只读契约 / 输出口径
# ═══════════════════════════════════════════════════════

def _tree_snapshot(root: Path) -> dict:
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            snap[str(p.relative_to(root))] = (
                p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha1(data).hexdigest())
        else:
            snap[str(p.relative_to(root))] = "dir"
    return snap


def test_check_all_is_read_only(chk, good_root):
    """不许写任何文件、不许改任何配置——被控制台按钮和 体检.py 调用时不能有副作用。"""
    before = _tree_snapshot(good_root)
    _run(chk, good_root, 5099, 3000, 3001)
    assert _tree_snapshot(good_root) == before


def test_probe_is_injectable_and_actually_used(chk, good_root):
    """端口探测必须是注入的——否则测试结果取决于跑测试时本机开没开 SnowLuma。"""
    calls = []

    def spy(host, port):
        calls.append((host, port))
        return port == 3001

    results = chk.check_all(good_root, port_probe=spy, environ={})
    assert (("127.0.0.1", 3001) in calls) and (("127.0.0.1", 3000) in calls)
    assert _get(results, chk.KEY_SUGAR_LISTEN).status is chk.Status.OK
    assert _get(results, chk.KEY_RUNNING).status is chk.Status.FAIL


def test_source_has_no_win10_unrenderable_glyphs(chk):
    """状态信息一律 bracket 家族——Win10 传统 conhost 画不出 emoji。"""
    text = TOOL.read_text(encoding="utf-8")
    for bad in ("✅", "❌", "⬜", "⚠", "ℹ", "⭐", "🩺"):
        assert bad not in text, f"tools/检查连接.py 里有 Win10 控制台画不出的字符：{bad}"


def test_main_prints_a_readable_checklist(chk, good_root, capsys, fake_ports):
    fake_ports()  # 端口全关：会 FAIL，恰好覆盖 [×] 分支
    code = chk.main([str(good_root)])
    out = capsys.readouterr().out
    assert code == 1
    assert "[√] 1. " in out and "[×] 2. " in out
    assert "-> " in out            # 每个非 OK 项都要有能照做的下一步
    assert "卡在第 2 步" in out
    assert "[·]" in out


# ═══════════════════════════════════════════════════════
# 拆开的纯函数（判据能单独测）
# ═══════════════════════════════════════════════════════

def test_resolve_token_matches_main_py_rules(chk):
    # main.py 的正则只认整串 ${NAME}
    tv = chk.resolve_token("${SNOWLUMA_TOKEN}", {"SNOWLUMA_TOKEN": "secret123"})
    assert (tv.value, tv.var_name, tv.is_unresolved_placeholder) == ("secret123", "SNOWLUMA_TOKEN", False)

    # 缺失时保留字面量（不是空串）——这正是必须单独报的一项
    tv = chk.resolve_token("${SNOWLUMA_TOKEN}", {})
    assert tv.value == "${SNOWLUMA_TOKEN}" and tv.is_unresolved_placeholder

    # 部分包裹 / 字面量一律原样
    assert chk.resolve_token("abc${X}def", {"X": "1"}).value == "abc${X}def"
    assert chk.resolve_token("plain-token", {}).value == "plain-token"
    assert chk.resolve_token(None, {}).value == ""


@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    ("short", "****"),
    ("8bzJFAKEtoken~oW", "8bzJ****oW"),
])
def test_mask_token(chk, raw, expected):
    assert chk.mask_token(raw) == expected


@pytest.mark.parametrize("url,expected", [
    ("ws://127.0.0.1:3001/ws", ("127.0.0.1", 3001)),
    ("ws://localhost:3001", ("127.0.0.1", 3001)),
    ("127.0.0.1:3001", ("127.0.0.1", 3001)),
    ("wss://example.com", ("example.com", 443)),
    ("ws://example.com/x", ("example.com", 80)),
    ("", None),
    (":::", None),
])
def test_parse_endpoint(chk, url, expected):
    assert chk.parse_endpoint(url) == expected


def test_version_key_orders_numerically(chk):
    names = ["SnowLuma-v1.9.0-win-x64", "SnowLuma-v1.14.9-win-x64",
             "SnowLuma-v1.11.4-win-x64-lite", "junk"]
    assert sorted(names, key=chk.version_key)[-1] == "SnowLuma-v1.14.9-win-x64"


def test_read_json_object_survives_garbage(chk, tmp_path):
    bad = tmp_path / "onebot_bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert chk.read_json_object(bad) == {}
    assert chk.read_json_object(tmp_path / "nope.json") == {}
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2,3]", encoding="utf-8")
    assert chk.read_json_object(arr) == {}


def test_network_lists_tolerates_missing_and_wrong_types(chk):
    got = chk.network_lists({"networks": {"wsClients": [{"url": "x"}, "junk", None]}})
    assert got["wsClients"] == [{"url": "x"}]
    assert got["httpServers"] == []
    assert chk.network_lists({})["wsServers"] == []
    assert chk.network_lists({"networks": "nope"})["wsClients"] == []


def test_build_env_prefers_process_env_over_env_file(chk, tmp_path):
    _write_env(tmp_path, SNOWLUMA_TOKEN="from-file")
    assert chk.build_env(tmp_path, {})["SNOWLUMA_TOKEN"] == "from-file"
    assert chk.build_env(tmp_path, {"SNOWLUMA_TOKEN": "from-env"})["SNOWLUMA_TOKEN"] == "from-env"


# ═══════════════════════════════════════════════════════
# 9. 接入点：docstring 声称谁调用它，就得真的有人调用
# ═══════════════════════════════════════════════════════

def test_declared_callers_actually_call_it():
    """`检查连接.py` 的 docstring 写着「调用方：控制台按钮、tools/体检.py、命令行」。

    这句必须是真的。2026-09-20 第一次写的时候，docstring 里列了 `体检.py`，
    而 `体检.py` 全文对 `检查连接` **零命中**——新代码自己犯了「文档说的和代码做的分家」
    （CLAUDE.md 反模式 #35）。这条闸门把三个接入点都钉住。
    """
    from pathlib import Path as _P
    base = _P(__file__).resolve().parent.parent

    console = (base / "糖糖控制台_qt.py").read_text(encoding="utf-8")
    assert "检查连接.py" in console, "控制台的「连接自检」按钮不再加载这个模块了"

    health = (base / "tools" / "体检.py").read_text(encoding="utf-8")
    assert "检查连接" in health and "check_all" in health, (
        "体检.py 没有调用链路自检——而检查连接.py 的 docstring 里写着它会调")


def test_lite_package_without_node_is_called_out(chk, tmp_path):
    r"""`-lite` 包不带 Node.js，必须单独报出来。

    2026-09-20 审查发现：发布页的资产列表里 `SnowLuma-vX.Y.Z-win-x64-lite.zip`
    按字典序**排在完整包前面**，新手很容易点错那个。lite 包里 `launcher.bat` 是有的，
    所以只判「launcher.bat 在不在」会给他一个 [√]，下一步又让他「再点一次启动」——
    而死循环的真因是 `launcher.bat` 第一行的 node 命令报了
    「'node' 不是内部或外部命令」，然后 pause 停住。
    """
    _make_snowluma(tmp_path, node=False)
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_INSTALLED)
    assert r.status is chk.Status.WARN
    assert "lite" in (r.detail + r.next_step).lower(), \
        f"没点明是 -lite 包：{r.detail} / {r.next_step}"
    assert "node.exe" in r.next_step, f"没告诉他缺的是 node.exe：{r.next_step}"


def test_missing_snowluma_does_not_tell_user_to_doubleclick_a_folder(chk, tmp_path):
    """还没装时，第 2 步不该让用户去双击一个**目录**。

    审查实测：原先第 2 步拼进去的落点是 `<项目根>/SnowLuma`（目录），
    双击只会打开资源管理器，而括号里的错指引会把正确的「点控制台按钮」稀释掉。
    最自然的顺序恰恰是先点自检、再去装。
    """
    _write_config(tmp_path)
    r = _get(_run(chk, tmp_path), chk.KEY_RUNNING)
    assert r.status is chk.Status.SKIP, \
        f"还没装 SnowLuma 却在断言「在不在跑」（{r.status}）——应当 SKIP"
    assert str(tmp_path / "SnowLuma") not in (r.next_step or ""), \
        f"又在让用户双击目录了：{r.next_step}"
