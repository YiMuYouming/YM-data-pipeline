# 开发计划

## 批1 ✅ 核心基座 — 已完成
- [x] 仓库初始化 + pyproject.toml
- [x] sources/pytdx.py（PyTDX 全新重写，6个数据接口）
- [x] sources/iwencai.py（问财模块化，支持通用查询+批量个股）
- [x] sources/ths_hot.py / tencent.py / eastmoney.py / northbound.py（复制管道源码）
- [x] sources/ths_industry.py（同花顺行业板块直连，替代akshare）
- [x] fetch.py 统一路由（12个data_type）

## 批2 对比验证 + 看板切流
- [ ] scripts/compare.py（新老系统输出对照）
- [ ] poll_live.py 改造为调用 ym_stock_data
- [ ] W10 板块热力新增净流入维度

## 批3 研报/公告/新闻
- [ ] sources/research.py（东财研报API）
- [ ] sources/filings.py（巨潮公告）
- [ ] sources/news.py（东财/财联社新闻）
