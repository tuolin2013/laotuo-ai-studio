"""门面页的报价页：首页有入口，页面上的数字与代码一致，联系方式与界面同步，不写无法兑现的承诺。"""

import json
import re
from pathlib import Path

from app.engine.config_loader import load_platforms
from app.providers.llm_providers import PROVIDERS
from app.schemas.plan import MAX_CANDIDATES_PER_SLOT
from app.ui.pro_panel import CONTACTS

LANDING = Path(__file__).resolve().parents[1] / "deploy" / "vercel-landing"
PRICING = (LANDING / "pricing.html").read_text(encoding="utf-8")
# 公开页面上不能出现的说法：绝对化承诺、与开源许可证冲突的「买断」
BANNED = ["100%", "永久", "买断", "彻底", "锁死", "绝不", "降级", "多角色", "尾帧"]


def test_index_links_to_pricing_in_header_and_footer():
    index = (LANDING / "index.html").read_text(encoding="utf-8")
    header = index.split("</header>")[0]
    footer = index.split("<footer>")[1]
    assert 'href="pricing.html"' in header
    assert 'href="pricing.html"' in footer
    assert 'href="index.html"' in PRICING


def test_pricing_has_no_unbackable_claims():
    # 只查 <body> 里给人看的文字：样式表里有 width: 100%，注释里列举了禁用说法
    body = PRICING.split("<body>", 1)[1]
    visible = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    for word in BANNED:
        assert word not in visible, word


def test_pricing_numbers_match_config():
    taobao = load_platforms()["taobao"]
    assert f"淘宝默认 {taobao.main_image.default_count} 张" in PRICING
    assert f"淘宝默认 {taobao.detail_page.default_screens} 屏" in PRICING
    assert f"1~{MAX_CANDIDATES_PER_SLOT} 张候选图" in PRICING


def test_pricing_llm_providers_exist():
    labels = " ".join(p.label for p in PROVIDERS)
    for name in ["DeepSeek", "通义千问", "智谱", "豆包", "Kimi", "OpenRouter"]:
        assert name in labels, name
        assert name in PRICING, name


def test_cloudflare_headers_match_vercel_json():
    """Cloudflare Pages 读 _headers，Vercel 读 vercel.json：两边的响应头一致。"""
    rules: dict[str, dict[str, str]] = {}
    path = None
    for line in (LANDING / "_headers").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" "):
            path = line.strip()
            rules[path] = {}
        else:
            name, value = line.strip().split(": ", 1)
            rules[path][name] = value
    vercel = json.loads((LANDING / "vercel.json").read_text(encoding="utf-8"))
    expected = {
        h["source"].replace("(.*)", "*"): {x["key"]: x["value"] for x in h["headers"]} for h in vercel["headers"]
    }
    assert rules == expected


def test_pricing_contacts_match_pro_panel_and_readme():
    pills = dict(re.findall(r'<span class="slot-pill">(\S+?)：(.+?)</span>', PRICING))
    assert pills == dict(CONTACTS)
    readme = (LANDING.parents[1] / "README.md").read_text(encoding="utf-8")
    for name, value in CONTACTS:
        assert f"> - {name}：`{value}`" in readme, name
