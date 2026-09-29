# 批次订单清洗与汇总

读取本次提供的本地订单 CSV，生成一张清洗订单表、一张客户汇总表和一份质量统计。source_file 是每次调用的参数，各批独立运行，方法可以反复用于不同输入。

CSV 包含 order_id,customer_id,revision,gross_cents,refund_cents 五列。revision、gross_cents、refund_cents 都是整数，金额单位为分。不得使用浮点金额计算、四舍五入或根据其他记录猜测缺失值。

customer_id 只去除首尾空白，其他字符不变；订单号、修订号及金额不作改写。

同一个 order_id 的所有修订都要参与客户一致性检查。任意修订的规范化 customer_id 与其他修订不一致，就拒绝这次处理。

每个订单在清洗表中只能占一行，添加 net_cents = gross_cents - refund_cents，结果按 order_id 升序。

按规范化 customer_id 对清洗后的订单分组，输出客户订单数和 gross、refund、net 三项金额合计，客户行按 customer_id 升序。

质量统计包含输入行数、保留订单数、移除行数及保留订单的 gross/refund/net 总额。检查方须从原始输入独立重算清洗、客户汇总与统计结果，并核对三者一致性。

清洗可以通过受约束的 Pi 任务及经过检查的子方法完成；固定业务能力的实现和验收要求不得由模型改写。

权限限于读取选定的本地输入及写出本次运行目录中的产物。输入不能被修改，不得删除文件，不得上传、发起网络业务访问或产生其他外部效果。
