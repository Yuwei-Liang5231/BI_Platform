"""AI 原生 BI 平台 后端应用包。

架构约束（架构 V1.3 不变式 7）：本包内禁止任何行业分支、行业专属字段或
硬编码业务规则。行业语义只允许存在于数据层（datasets / dataset_relations /
dataset_coverage）与配置层（calc_rule、指标模板、topic 枚举）。
"""

__version__ = "0.1.0"
