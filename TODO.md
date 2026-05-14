# 开发计划

## 批1 ✅ 核心基座
- [x] 仓库初始化 + pyproject.toml
- [x] 7个数据源全部通过验证
- [x] fetch.py 统一路由（12个data_type）
- [x] iwencai.py 按 iwencai-data Skill 规范修正

## 批2 ✅ 对比验证 + 最佳实践对标
- [x] compare.py 新老系统对比（5维度全通过）
- [x] 对标 simonlin1212/a-stock-data 最佳实践
- [x] 对标 PyTDX API 文档 (45字段确认)
- [x] iwencai 新增 comprehensive/search (代码就绪，待API key升级)
- [x] tencent 字段映射确认为正确 (v39=PE, v43=振幅, v46=PB)
- [x] 提取 _iwencai_headers() 消除重复

## 批3 ✅ 看板接入 + L4研报/公告/新闻
- [x] consumer/dashboard.py 看板数据适配器
- [x] research.py 东财研报 (reportapi.eastmoney.com)
- [x] filings.py 巨潮公告 (cninfo.cn, PDF下载)
- [x] news.py 财联社新闻 (cls.cn, 分钟级)
- [x] fetch.py 新增 L4 路由 (15个data_type)
- [x] 全量测试: 14/14 通过

## 后续 (pending)
- [ ] poll_live.py 切流（改为调用 ym_stock_data）
- [ ] W10 板块热力新增净流入维度
- [ ] iwencai comprehensive/search API key权限升级
