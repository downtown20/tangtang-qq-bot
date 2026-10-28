"""出厂 config.example.yaml 的密钥字段契约（2026-09-20）。

## 这个文件为什么必须存在

2026-09-20 主人实机反馈：「控制台输入的 API Key 不会到 config 生效」。
根因是 `tools/准备发布.py` 的 `scrub_cfg()` 把开发机 config.yaml 里**正确的**
`${DEEPSEEK_KEY}` / `${SNOWLUMA_TOKEN}` 主动改写成了「你的DeepSeek或Claude的API密钥」
「请修改为你自己的 access_token」这类**中文字面量**。

后果链（每一环都单独正确，合起来全错）：
  1. 控制台「🔑 密钥管理」只写 `.env`（它从不写 config.yaml 的密钥字段）
  2. 运行期 `main._resolve_env_vars` 只认"整串恰好是 `${VAR}`"，字面量一个字符都不替换
  3. → config.yaml 里的字面量赢，`.env` 从被创建起就没人读过
  4. 而界面上一切正常：保存成功、字段回显正确、弹窗说「已保存」

唯一的例外是 provider=deepseek（`agent/handler.py` 有一行特判把它捞了回来），
所以真实症状更怪：**默认配置能用，换个 provider 就 401**。

## 为什么原来三道闸门都没拦住

  · `tests/test_secret_config.py` 断言的是 `parents[1]/config.yaml`——**开发树的源文件**。
    它按构造必然含 `${VAR}`，所以永远绿；而出厂模板（真产物）没人管。
    这正是 CLAUDE.md 反模式 #30 的句子：「测试样本必须来自真产物」。
  · `tools/体检.py` 的 `check_secrets` 只枚举 config.yaml 里**已是 `${VAR}`** 的引用——
    形态错了的那些它压根不看，静默通过。
  · `tests/test_installer_wiring.py` 自己写一份合成模板，里面**根本没有 api_key 这一项**。

三道闸门、三个不同的"看不见"。

## 阳性对照怎么做的

不能靠"往输出里种一个坏值再断言"——`scrub_cfg` 会把它覆盖掉，
测出来的是"函数会覆盖字段"而不是"闸门认得出坏形态"。
所以本文件把**判据抽成 `_template_violations(scrub_fn)`**，然后用**旧版 scrub_cfg**
（逐字复刻那三行 bug）跑同一个判据，断言它必须报出违规。
判据改了、旧实现却"通过"了，说明判据已经不认这个形状了——那才是真正要防的退化。
"""
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parent.parent
PUBLISH_TOOL = BASE / "tools" / "准备发布.py"


def _publish_module():
    """加载 tools/准备发布.py（模块级只有常量，`main()` 在 __main__ 守卫里）"""
    spec = importlib.util.spec_from_file_location("_publish_tool", PUBLISH_TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _real_config() -> dict:
    """开发机的 config.yaml——scrub_cfg 的输入。没有就跳过（干净 clone）。"""
    path = BASE / "config.yaml"
    if not path.is_file():
        pytest.skip("没有 config.yaml（干净 clone）——本契约测的是 scrub_cfg 的输出")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _dig(data, path):
    """按路径取值；任一层缺失返回 None。"""
    node = data
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _legacy_scrub_cfg(cfg: dict, real_config: dict) -> dict:
    """**旧版** scrub_cfg 的密钥三行——逐字复刻 2026-09-20 之前的行为。

    只用于阳性对照。它存在的唯一理由是回答「闸门认得出这个形状吗」——
    如果没有它，本文件测的就只是"开发者机 config.yaml 恰好是对的"。
    """
    import copy
    c = copy.deepcopy(real_config)
    c.setdefault("llm", {})["api_key"] = "你的DeepSeek或Claude的API密钥"
    if isinstance(c["llm"].get("vision"), dict):
        c["llm"]["vision"]["api_key"] = "你的视觉API密钥（不用识图可留空）"
    c.setdefault("napcat", {})["access_token"] = "请修改为你自己的 access_token"
    return c


def _template_violations(scrub) -> list:
    """出厂模板契约的**判据本身**。返回违规清单（空 = 通过）。

    抽成独立函数是为了让阳性对照能用同一个判据跑旧实现——
    判据写在两条 test 里各一份的话，对照就测不到它们。
    """
    from agent.secret_refs import LEGACY_SECRET_LITERALS, secret_paths
    cfg = scrub(_real_config())
    text = yaml.dump(cfg, allow_unicode=True, default_flow_style=False, sort_keys=False)
    bad = []
    for path, env_name in secret_paths(cfg):
        cur = _dig(cfg, path)
        if cur != "${" + env_name + "}":
            bad.append(f"{'.'.join(path)} = {cur!r}（应为 ${{{env_name}}}）")
    bad += [f"残留老占位串：{s}" for s in LEGACY_SECRET_LITERALS if s in text]
    return bad


# ═══════════════════════════════════════════════════════════
# 正面：出厂模板的密钥字段必须是 ${VAR}
# ═══════════════════════════════════════════════════════════

def test_factory_template_keeps_secrets_as_env_references():
    """`scrub_cfg` 的输出里，四个密钥字段都必须是 `${VAR}`。

    这是 `.env` 机制唯一的入口。写成字面量等于把机制整个架空：
    控制台写 .env、运行期读 config.yaml，两边永不接头。
    """
    mod = _publish_module()
    bad = _template_violations(mod.scrub_cfg)
    assert not bad, "出厂模板的密钥字段不是 ${VAR} 引用：\n  " + "\n  ".join(bad)


def test_env_example_covers_every_reference_in_template():
    """模板里出现的每个 `${VAR}`，`.env.example` 里都得有同名的一行。

    没有这一条，用户看到 `api_key: ${QWEN_KEY}` 却不知道该往 .env 里写什么名字——
    而早先的 `.env.example` 只有 `SNOWLUMA_TOKEN=` 一行（Code Review B-6）。
    """
    mod = _publish_module()
    text = yaml.dump(mod.scrub_cfg(_real_config()), allow_unicode=True,
                     default_flow_style=False, sort_keys=False)
    refs = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", text))

    # 2026-10-28：排除「provider 组自动生成的 KEY」。
    # 前端在新建 provider 时会落一个 `${<组id大写>_KEY}` 引用（例如组 id 是
    # `pmvro2c5y` → `${PMVRO2C5Y_KEY}`），那个名字**每次新建都变**，写进
    # `.env.example` 毫无意义（用户照着抄下一行，代码里也没有对应读取点）。
    # 判据按前端生成规则精确匹配，不是「凡是 _KEY 结尾都放过」——
    # 用户手写的 `${MY_API_KEY}` 仍然要求出现在模板里。
    providers = ((_real_config().get("llm") or {}).get("providers") or [])
    auto = {
        f"{str(p.get('id', '')).upper()}_KEY".replace("-", "_")
        for p in providers
        if isinstance(p, dict) and str(p.get("id") or "").strip()
    }

    gen = re.search(r'\(OUT / "\.env\.example"\)\.write_text\((.*?)\n\s+encoding="utf-8"\)',
                    PUBLISH_TOOL.read_text(encoding="utf-8"), re.S)
    assert gen, "找不到 .env.example 的生成处——本闸门失去了锚点"
    example = gen.group(1)
    missing = sorted(r for r in refs - auto if f"{r}=" not in example)
    assert not missing, (
        f".env.example 缺这些键：{missing}——模板引用了它们，"
        f"用户却不知道变量该叫什么名字")


def test_env_example_lists_every_provider_variable():
    """`.env.example` 必须把**全部五个** provider 变量都列出来。

    用户的 provider 不一定是 deepseek；模板里少一个名字，他就少一条
    「我该填哪个变量」的线索——而填错变量名的症状是"填了也不生效"，
    和本次事故一模一样。（Reality Check D6 指出这四个键当时没人钉）
    """
    from agent.secret_refs import PROVIDER_KEY_ENV
    src = PUBLISH_TOOL.read_text(encoding="utf-8")
    gen = re.search(r'\(OUT / "\.env\.example"\)\.write_text\((.*?)\n\s+encoding="utf-8"\)',
                    src, re.S)
    assert gen, "找不到 .env.example 的生成处"
    for name in PROVIDER_KEY_ENV.values():
        assert f"{name}=" in gen.group(1), f".env.example 没列 {name}"


def test_template_says_memory_sync_default_visible():
    """模板里要能**看见** `memory_sync.auto`（用户看不见的开关等于不存在）。"""
    mod = _publish_module()
    assert mod.scrub_cfg(_real_config()).get("memory_sync") == {"auto": True}


def test_provider_key_env_names_match_console_and_runtime():
    """`PROVIDER_KEY_ENV` 是控制台写 .env、运行期回填、模板生成共用的那张表。

    两边错位 = 用户在控制台填的 Key 落进一个没人读的变量名里
    （正是 `_on_provider_changed` 改了 provider 却仍写旧变量名那个 bug 的形态，
     2026-09-20 已修）。
    """
    from agent.secret_refs import FIXED_SECRET_PATHS, PROVIDER_KEY_ENV
    # 2026-10-26：Qt 控制台从根目录移进 `运维/`（功能保留，根目录不再有它）。
    # 这里**只认一个位置**：双路径兜底会让「有人把它搬回根目录」也悄悄通过，
    # 而根目录规范化正是 2026-10-26 那一轮的核心目标（教训 #35：文档/约定当代码测）。
    console_path = BASE / "运维" / "糖糖控制台_qt.py"
    assert console_path.is_file(), f"找不到 Qt 控制台（应该在 运维/ 下）：{console_path}"
    assert not (BASE / "糖糖控制台_qt.py").exists(), (
        "根目录又出现 QT 控制台了——2026-10-26 已定：它只住在 运维/")
    console = console_path.read_text(encoding="utf-8")
    handler = (BASE / "agent" / "handler.py").read_text(encoding="utf-8")
    assert "PROVIDER_KEY_ENV.get(_provider" in handler, (
        "handler.py 又写死了 provider→变量名的对应——换 provider 就会 401")
    assert "PROVIDER_KEY_ENV" in console, "控制台没接上变量名单一事实源"
    assert set(PROVIDER_KEY_ENV) == {"deepseek", "xai", "anthropic", "openai", "custom"}
    assert {"SEDUCTIVE_KEY", "QWEN_KEY", "SNOWLUMA_TOKEN"} == {n for _, n in FIXED_SECRET_PATHS}


# ═══════════════════════════════════════════════════════════
# 阳性对照：把旧的 scrub_cfg 换回去，闸门必须变红
# ═══════════════════════════════════════════════════════════

def test_gate_goes_red_on_the_legacy_scrubber():
    """用**旧版** scrub_cfg 跑同一个判据——必须报出违规。

    这条不测生产代码，测的是**判据本身还有没有分辨力**。
    只跑"没报错"等于没测：检查器本来就不会报错。
    """
    bad = _template_violations(lambda _cfg: _legacy_scrub_cfg(_cfg, _real_config()))
    assert bad, (
        "阳性对照失败：把 2026-09-20 之前那三行 bug 换回去，判据居然还是绿的——"
        "说明它已经不认这个形状了，等于没有闸门")
    joined = "\n".join(bad)
    assert "llm.api_key" in joined and "access_token" in joined, (
        f"阳性对照只抓到一部分：\n{joined}")


def test_gate_goes_red_on_a_materialized_key():
    """哪怕值不是中文占位串，只要不是 `${VAR}` 就该红（防的是"物化成明文"）。"""
    def _materializing(_cfg):
        import copy
        c = copy.deepcopy(_real_config())
        c.setdefault("llm", {})["api_key"] = "sk-looks-real-but-is-not-a-reference"
        return c

    bad = _template_violations(_materializing)
    assert any("llm.api_key" in b for b in bad), (
        "阳性对照失败：物化后的明文没被认出来")


# ═══════════════════════════════════════════════════════════
# 干净 clone 也必须能验证的一条（2026-10-26）
# ═══════════════════════════════════════════════════════════

def test_config_example_exists_in_repo_even_without_real_config():
    """模板文件本身必须在仓库里——这条**不依赖**开发机的 config.yaml。

    本文件其余测试都从 `_real_config()` 出发（没有 config.yaml 就 skip），
    所以「config.example.yaml 根本不在仓库里」这个缺口在干净 clone / CI 上
    永远是绿的——正是教训 #30/#37 的假绿。这条只认仓库里的文件。
    """
    example = BASE / "config.example.yaml"
    assert example.is_file(), (
        "仓库里没有 config.example.yaml——干净 clone 的用户永远没有配置，"
        "tools/安装糖糖.py 的 bootstrap_config() 也只能静默跳过")
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, "config.example.yaml 解析出来不是非空 dict"
    api_key = str(data.get("llm", {}).get("api_key", ""))
    assert re.fullmatch(r"\$\{[A-Z_][A-Z0-9_]*\}", api_key), (
        f"模板的 llm.api_key 不是 ${{VAR}} 引用（被物化成字面量了）：{api_key!r}")
