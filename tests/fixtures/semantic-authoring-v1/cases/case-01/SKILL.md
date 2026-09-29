# 订单整理作业

每次接收一份本地订单 CSV，交付清洗明细、客户汇总和数据质量统计。将 source_file 留作本次运行的输入参数；不同批次独立处理，不把某一批的数据或路径写入通用方法。

输入恰有 order_id、customer_id、revision、gross_cents、refund_cents 五列。revision 是整数修订号，gross_cents 和 refund_cents 是整数分。运算使用整数，不做浮点金额计算、四舍五入或缺失值猜补。

只去掉 customer_id 两端的空白，保留标识中间的所有字符。不得改写 order_id、revision 或金额字段。

先检查同一 order_id 的全部修订：规范化后的 customer_id 只要有两个不同值，就拒绝本批处理，不能只看将要保留的那一行。

每个订单保留整数 revision 最大的一条，而不是输入中排在最后的一条。最大 revision 若并列，规范化后的客户与金额完全相同可以合为一条；若这些内容冲突，则拒绝处理。

清洗表每个 order_id 恰好一行，增加 net_cents = gross_cents - refund_cents，按 order_id 升序排列。

客户汇总按规范化的 customer_id 分组，列出订单数、gross_cents 合计、refund_cents 合计和 net_cents 合计，按 customer_id 升序排列。

质量统计给出原输入行数、保留订单数、移除行数以及清洗结果的 gross、refund、net 合计。验收必须从原始输入及上述保留规则独立重算，逐项核对清洗明细、客户汇总和质量统计，不能只比较某一张表的总额。

全部业务步骤必须使用已声明、已注册的固定实现。模型不能生成替代代码，也不能修改固定程序。

只读本次本地输入，输出只写入本次运行的产物目录。不得修改输入、删除文件、联网传送、上传或产生其他外部业务效果。
