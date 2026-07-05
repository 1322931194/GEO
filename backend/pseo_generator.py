"""
见微 · pSEO 批量行业页生成器
================================================
用「城市 × 行业模板」程序化生成大批 GEO 行业页数据。

设计原则：
1. 手写精品页（pseo.py 里的 INDUSTRY_DB）优先级最高，不被覆盖；
2. 批量页用行业模板 + 城市变量填充，一次可生成几百页；
3. 每个行业模板有独立的痛点/策略骨架，避免全站内容雷同（AI/SEO 会惩罚重复内容）；
4. 生成结果可直接被 pseo.py 引用。

用法（在 pseo.py 里）：
    from pseo_generator import build_generated_db
    GENERATED_DB = build_generated_db()
    # 合并：手写优先
    FULL_DB = {**GENERATED_DB, **INDUSTRY_DB}
"""

# ============================================================
# 城市池：可自由增删。批量生成 = 城市 × 行业
# ============================================================
CITIES = [
    "深圳", "上海", "北京", "广州", "杭州", "成都",
    "武汉", "南京", "苏州", "重庆", "西安", "长沙",
]

# ============================================================
# 行业模板：每个行业一套「差异化」的痛点/策略骨架。
# {city} 会被替换成具体城市，保证每页内容不同、不雷同。
# slug_key 用于生成 URL：{slug_key}-{city_pinyin}
# ============================================================
INDUSTRY_TEMPLATES = {
    "home-decoration": {
        "industry": "装修公司",
        "city_scene": "业主装修",
        "pain_points": [
            "业主装修前必问AI「{city}装修哪家公司靠谱」。AI报的是那几家老对手，你的案例和口碑在答案里隐形——高意向、高客单的业主就这么被推给了别人。",
            "你的施工案例、业主好评都在官网和朋友圈里，AI抓不到、不采信。家装是重信任决策，缺乏第三方语料佐证，AI宁可不推你。",
            "获客成本一年比一年高，而AI推荐是免费的信任背书。你没占住，等于把{city}最优质的免费获客入口拱手让给同行，业主商机持续蒸发。",
        ],
        "strategy": "见微针对{city}装修公司，围绕「业主AI决策+口碑建立」双线优化。先扫描AI在各类装修咨询上对你的可见度、竞品抢占，以及是否存在说错你信息的风险。随后帮你在AI信任的信源布局客观的装修科普与真实案例语料，既提升被推荐概率，也校准AI对你的认知。以7天为周期监测优化，让{city}业主问AI时优先看到准确的你，把高价值业主稳稳接住。",
    },
    "dental-clinic": {
        "industry": "口腔诊所",
        "city_scene": "患者看牙",
        "pain_points": [
            "患者做种牙、正畸前必问AI「{city}种牙/正畸哪家好」。AI报的是那几家连锁，你的技术和口碑在答案里隐形——高客单的到诊客户被同行拿走。",
            "你的医生资质、真实案例都在诊所官网里，AI抓不到、不采信。口腔医疗是重信任高客单决策，缺乏第三方权威语料，AI不敢推你、甚至可能说错你的项目价格。",
            "投流获客越来越贵，转化越来越难。AI推荐这个免费的信任入口你没占住，{city}高价值患者持续被对手的语料库截流。",
        ],
        "strategy": "见微针对{city}口腔诊所，围绕「患者AI决策+医疗信任」双线优化。先扫描AI在种牙、正畸等项目咨询上对你的可见度、竞品抢占，以及是否存在说错项目价格的风险。随后帮你在AI信任的信源布局合规、客观的口腔科普与真实案例语料，既提升被推荐概率，也防止AI乱说损害口碑。以7天为周期监测优化，让{city}患者问AI时优先看到准确的你，把高价值到诊客户接住。",
    },
    "medical-aesthetics": {
        "industry": "医疗美容",
        "city_scene": "求美者咨询",
        "pain_points": [
            "求美者做决定前必问AI「{city}做XX哪家正规」。AI报的是那几家老对手，你的机构没出现——高意向、高客单的到店客户，就这么被推给了别人。",
            "医美是重信任决策，AI尤其看重第三方口碑与合规信源。你缺乏被AI采信的正向语料，AI宁可不推，甚至可能把你和别家搞混、说错你的项目和价格。",
            "投流成本一年比一年高，转化却越来越难。而AI推荐是免费的信任背书，你没占住，等于把{city}最优质的免费获客入口，拱手让给了同行。",
        ],
        "strategy": "见微针对{city}医美机构，围绕「求美者AI决策+信任重建」双线优化。先扫描AI在各类项目咨询问题上对你的可见度、竞品抢占度，以及是否存在说错你信息的风险。随后帮你在AI信任的信源上布局合规、客观的科普与真实案例语料，既提升被推荐概率，也校准AI对你的认知。以7天为周期监测优化，让{city}求美者问AI时既能优先看到你、又能看到准确的你，把高价值到店客户稳稳接住。",
    },
    "pet-hospital": {
        "industry": "宠物医院",
        "city_scene": "宠主看诊",
        "pain_points": [
            "宠主给毛孩子看病前必问AI「{city}哪家宠物医院靠谱」。AI报的是那几家连锁，你的专业和口碑在答案里隐形——高粘性、高复购的宠主被同行拿走。",
            "你的医疗设备、专业案例、宠主好评都在朋友圈和官网里，AI抓不到、不采信。宠物医疗是重信任决策，缺乏第三方语料佐证，AI不敢推你。",
            "宠物经济高速增长，但AI推荐这个免费信任入口你没占住。{city}宠主越来越依赖AI找医院，你不在名单里，高价值客户持续被对手截流。",
        ],
        "strategy": "见微针对{city}宠物医院，围绕「宠主AI决策+专业口碑」双线优化。先扫描AI在各类宠物医疗咨询上对你的可见度、竞品抢占，以及是否存在说错你信息的风险。随后帮你在AI信任的信源布局客观的宠物健康科普与真实诊疗案例语料，既提升被推荐概率，也校准AI对你的认知。以7天为周期监测优化，让{city}宠主问AI时优先看到专业准确的你，把高粘性客户接住。",
    },
    "legal-service": {
        "industry": "法律服务",
        "city_scene": "当事人咨询",
        "pain_points": [
            "当事人遇到纠纷，第一时间问AI「{city}XX纠纷找哪个律师」。AI报的是那几家大所，你的专业和胜诉案例在答案里彻底隐形——高价值案源就这么流走了。",
            "你的胜诉案例、专业文章都躺在律所官网里，AI抓不到、也不采信。法律是极重信任的决策，缺乏第三方权威语料佐证，AI宁可不推你。",
            "高净值当事人越来越依赖AI初筛律师。你没进AI的推荐名单，等于在最关键的获客入口前就出局了，{city}优质案源持续被大所语料库截流。",
        ],
        "strategy": "见微针对{city}律所，聚焦「当事人AI咨询决策+专业信任建立」双线优化。先扫描AI在各类法律咨询问题上对你的可见度与竞品抢占情况，锁定你最擅长的专业领域精准词。随后帮你把胜诉案例、专业解读重构成AI偏爱的结构化语料，定向布局到知乎法律话题、专业问答平台等AI信任的信源。以7天为周期监测优化，让{city}当事人问AI时优先看到你的专业度，把高价值案源真正接住。",
    },
    "education-training": {
        "industry": "教育培训",
        "city_scene": "家长选课",
        "pain_points": [
            "家长给孩子报班前必问AI「{city}哪家培训机构好」。AI报的是那几家老牌，你的师资和口碑在答案里隐形——高意向的生源被同行拿走。",
            "你的师资、课程、学员成果都在官网和公众号里，AI抓不到、不采信。教培是重信任决策，缺乏第三方语料佐证，AI不敢推你。",
            "招生成本越来越高，而AI推荐是免费的信任入口。你没占住，{city}优质生源持续被对手的语料库截流。",
        ],
        "strategy": "见微针对{city}教培机构，围绕「家长AI决策+口碑建立」优化。先扫描AI在各类课程咨询上对你的可见度与竞品抢占，锁定你最强的科目精准词。随后帮你把师资、课程、学员成果重构成AI偏爱的结构化语料，布局到知乎、家长社区等AI信任信源。以7天为周期监测优化，让{city}家长问AI时优先看到你，把优质生源接住。",
    },
}

# 城市 → 拼音 slug（用于 URL）
_CITY_PINYIN = {
    "深圳": "shenzhen", "上海": "shanghai", "北京": "beijing", "广州": "guangzhou",
    "杭州": "hangzhou", "成都": "chengdu", "武汉": "wuhan", "南京": "nanjing",
    "苏州": "suzhou", "重庆": "chongqing", "西安": "xian", "长沙": "changsha",
}


def build_generated_db(cities=None, industries=None):
    """
    生成「城市 × 行业」组合页数据。
    cities: 城市列表，默认用 CITIES
    industries: 行业 key 列表，默认用全部 INDUSTRY_TEMPLATES
    返回: {slug: data_dict}
    """
    cities = cities or CITIES
    industries = industries or list(INDUSTRY_TEMPLATES.keys())
    db = {}
    for ind_key in industries:
        tpl = INDUSTRY_TEMPLATES.get(ind_key)
        if not tpl:
            continue
        for city in cities:
            py = _CITY_PINYIN.get(city)
            if not py:
                continue
            slug = f"{py}-{ind_key}"
            industry = tpl["industry"]
            db[slug] = {
                "slug": slug,
                "city": city,
                "industry": industry,
                "seo_title": f"{city}{industry}GEO优化方案 | 见微 - AI搜索获客破局专家",
                "seo_desc": f"{city}{industry}在AI搜索中被同行截流客户？见微GEO方案帮你解决「AI搜索隐形」难题，让客户问AI「{city}{industry}哪家好」时优先看到你，抢回被截流的高价值商机。",
                "pain_points": [p.format(city=city) for p in tpl["pain_points"]],
                "strategy": tpl["strategy"].format(city=city),
            }
    return db


if __name__ == "__main__":
    db = build_generated_db()
    print(f"批量生成 {len(db)} 个行业页：")
    print(f"  城市数: {len(CITIES)} × 行业数: {len(INDUSTRY_TEMPLATES)} = {len(db)} 页")
    # 抽样打印
    for i, (slug, d) in enumerate(db.items()):
        if i < 5:
            print(f"  · /solutions/{slug}  →  {d['city']}{d['industry']}")
    print(f"  ... 共 {len(db)} 个")
