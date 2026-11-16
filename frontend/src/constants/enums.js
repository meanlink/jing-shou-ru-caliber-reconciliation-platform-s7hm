export const Role={admin:'业务负责人',analyst:'分析员',viewer:'观察员'};
export const JobStatus={queued:'排队中',running:'计算中',completed:'已完成',failed:'执行失败'};
export const MasterStatus={active:'有效',deprecated:'已弃用'};
export const EntryResolution={auto:'别名自动归属',claimed:'人工认领',adjudicated:'合并逐笔裁决',pending:'待认领'};
export const ResolutionBasis={alias:'别名命中',claim:'人工认领',merge:'合并并入', 'merge-keep':'合并保留原主档',undo:'撤销还原','':''};
export const OpType={merge:'合并主档',undo:'撤销合并'};
export const OpStatus={running:'进行中',completed:'已完成',failed:'已阻止',undone:'已撤销'};
