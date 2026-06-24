# ===== AI 平台密钥(至少配一个,配越多监测越全)=====
OPENAI_API_KEY=sk-你的OpenAI密钥
GEMINI_API_KEY=你的Gemini密钥
ANTHROPIC_API_KEY=你的Anthropic密钥
PERPLEXITY_API_KEY=你的Perplexity密钥

# ===== 生成功能用的模型(默认 gpt-4o)=====
GEN_MODEL=gpt-4o

# ===== 数据库(默认SQLite,生产改成Postgres)=====
# DATABASE_URL=postgresql://user:pass@localhost:5432/geo_radar
DATABASE_URL=sqlite:///./geo_radar.db

# ===== 安全密钥(生产务必改成随机长字符串)=====
JWT_SECRET=请改成一段随机长字符串
