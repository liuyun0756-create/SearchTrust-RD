# P0-09 裁定后结果

状态：STOPPED_AFTER_ADJUDICATION。已停止；没有完整站点/竞品详情/渲染采集，没有最终获客生成输入，没有填写 truth。

累计 SerpAPI 76 次（首轮 34 + 本次 42）；Firecrawl 0 credits。保守用量预留 $1.90，剩余 109 次 / 30 credits / $3.10；实际账单未查询。

Fresno PR、All Star、Nova 三份新的市场观察均为 `36.7468422,-119.7725868`，距裁定参考点 1.638 公里。三站各自保存市场快照，不复写 Nova 的旧冻结报告。Hilo 为裁定坐标 `19.7241,-155.0868`，距离 0 公里。

Swansea 与 Pensacola 也限定 City 类型。Swansea 为 `51.62144,-3.943646`；Pensacola 为 `30.421309,-87.2169149`。后两站距离参考为首轮保存的解析点（不是另称为官方市中心）：分别约 0 / 3.480 公里。完整提供商记录和比较依据见 city-point-audit.json。

竞品仅取 Q1 maps，按来源顺序排除自身、目录/平台、缺官网、国家公园、地址无法确认目标城市、重复域名者，再取前三。每个入选记录保留 source_market_checksum / source_market_job_id 和源 record_id；正式 discovery/获客绑定待完整身份及后续流程，不伪造已完成绑定。

## PR Plumbing Fresno

GBP 状态：`confirmed_by_reviewer`。

| 名称 | 官网 | GBP | 电话 | 地址 |
| --- | --- | --- | --- | --- |
| PR Plumbing, Heating & Air Conditioning Inc. | https://www.prplumbingfresno.com/ | https://www.google.com/maps?cid=9308775580876614598 | (559) 244-0440 | 1504 N Maple Ave, Fresno, CA 93703 |

### 按裁定选出的 Q1 maps 竞品

| 顺位 | 商家 | 官网 | CID | 地址 |
| --- | --- | --- | --- | --- |
| 5 | Low Cost Plumbing Services | http://www.low-costplumbingservices.com/ | 2344010983889749812 | 2793 N Sunnyside Ave Ste 104, Fresno, CA 93727 |
| 6 | Fresno Plumbing & Heating Inc | https://www.fresnoplumbinginc.com/ | 12978353145315488362 | 2585 N Larkin Ave, Fresno, CA 93727 |
| 7 | Lee's Air, Plumbing, Heating, & Roofing | https://leesair.com/ | 8338722687629414135 | 2440 S Gearhart Ave #101, Fresno, CA 93725 |

入选 3 家；完整排除记录及快照引用见 `pr-plumbing-fresno/result.json`。

## All Star Plumbing Fresno

GBP 状态：`confirmed_by_reviewer`。

| 名称 | 官网 | GBP | 电话 | 地址 |
| --- | --- | --- | --- | --- |
| All Star Plumbing Fresno | http://www.allstarplumbingfresno.com/ | https://www.google.com/maps?cid=3959786852779329755 | (559) 445-9999 | 5639 W Barstow Ave, Fresno, CA 93722 |

### 按裁定选出的 Q1 maps 竞品

| 顺位 | 商家 | 官网 | CID | 地址 |
| --- | --- | --- | --- | --- |
| 1 | PR Plumbing, Heating & Air Conditioning Inc. | https://www.prplumbingfresno.com/ | 9308775580876614598 | 1504 N Maple Ave, Fresno, CA 93703 |
| 5 | Low Cost Plumbing Services | http://www.low-costplumbingservices.com/ | 2344010983889749812 | 2793 N Sunnyside Ave Ste 104, Fresno, CA 93727 |
| 6 | Fresno Plumbing & Heating Inc | https://www.fresnoplumbinginc.com/ | 12978353145315488362 | 2585 N Larkin Ave, Fresno, CA 93727 |

入选 3 家；完整排除记录及快照引用见 `all-star-plumbing-fresno/result.json`。

## Vision-Tech AV

GBP 状态：`confirmed_by_matching_rule`。

| 名称 | 官网 | GBP | 电话 | 地址 |
| --- | --- | --- | --- | --- |
| Vision Tech AV LTD | https://visiontechav.com/ | https://www.google.com/maps?cid=3467016953362037400 | 0333 335 5510 | Unit 9, Parc, Gelli Werdd, Cross Hands, Llanelli SA14 6EF |

### 有界查找记录

查询：`Vision-Tech AV Swansea`，返回状态：`found`。

| 候选 | 官网 | 电话 | 域名匹配 | 电话匹配 | 保留 |
| --- | --- | --- | --- | --- | --- |
| Vision Tech AV LTD | https://visiontechav.com/ | 0333 335 5510 | True | True | True |
| Haven Automation Limited | https://www.haven.co.uk/ | 01792 588722 | False | False | False |
| Wattech LTD | http://www.wattech.co.uk/ | 0800 031 4338 | False | False | False |

查找次数累计（含首轮）：2，上限 3。

### 按裁定选出的 Q1 maps 竞品

| 顺位 | 商家 | 官网 | CID | 地址 |
| --- | --- | --- | --- | --- |
| 1 | Pro Audio Visual | http://www.pro-audio-visual.co.uk/ | 12443407556994753755 | Unit 12b, Prospect Park, Fforest-fach, Swansea SA5 4ED |
| 2 | Audio T Swansea | http://www.audiot.co.uk/storefinder/swansea/ | 11476941075902233157 | 9 High St, Swansea, SA1 1LE |
| 3 | Asquith Electrical and Sons LTD | http://www.asquithelectrical.co.uk/ | 17932276471465453221 | 10, 11 Caer St, Swansea, SA1 3PP |

入选 3 家；完整排除记录及快照引用见 `vision-tech-swansea/result.json`。

## Seamless Gutters

GBP 状态：`gbp_not_found`。

### 有界查找记录

查询：`Seamless Gutters Pensacola`，返回状态：`found`。

| 候选 | 官网 | 电话 | 域名匹配 | 电话匹配 | 保留 |
| --- | --- | --- | --- | --- | --- |
| Martin's Seamless Gutters & Fencing | https://www.martinsseamlessgutters.com/?utm_source=google&utm_medium=organic&utm_campaign=gbp&utm_content=website_button | (850) 324-8000 | False | False | False |
| Pensacola Seamless Gutters | http://pensacolaseamlessgutters.com/ | (850) 898-9352 | False | False | False |
| The Brothers That Just Do Gutters | https://pensacola.brothersgutters.com/?utm_source=GMB&utm_medium=organic?utm_source=GMB&utm_medium=organic | (850) 462-4680 | False | False | False |
| Escambia Seamless Gutters | — | (850) 450-0255 | False | False | False |
| Premier Gutters LLC | http://www.premierguttersllc.com/ | (850) 396-1835 | False | False | False |
查询：`Seamless Gutters Pensacola FL`，返回状态：`found`。

| 候选 | 官网 | 电话 | 域名匹配 | 电话匹配 | 保留 |
| --- | --- | --- | --- | --- | --- |
| Martin's Seamless Gutters & Fencing | https://www.martinsseamlessgutters.com/?utm_source=google&utm_medium=organic&utm_campaign=gbp&utm_content=website_button | (850) 324-8000 | False | False | False |
| Pensacola Seamless Gutters | http://pensacolaseamlessgutters.com/ | (850) 898-9352 | False | False | False |
| The Brothers That Just Do Gutters | https://pensacola.brothersgutters.com/?utm_source=GMB&utm_medium=organic?utm_source=GMB&utm_medium=organic | (850) 462-4680 | False | False | False |
| Escambia Seamless Gutters | — | (850) 450-0255 | False | False | False |
| Into Gutters | https://intogutters.com/ | (850) 860-3606 | False | False | False |

查找次数累计（含首轮）：3，上限 3。

**输入缺口：gbp_not_found。后续不能用虚构 GBP 跑完整生成。**

### 按裁定选出的 Q1 maps 竞品

| 顺位 | 商家 | 官网 | CID | 地址 |
| --- | --- | --- | --- | --- |
| 1 | Martin's Seamless Gutters & Fencing | https://www.martinsseamlessgutters.com/?utm_source=google&utm_medium=organic&utm_campaign=gbp&utm_content=website_button | 947287778014820969 | 4601 W Jackson St, Pensacola, FL 32506 |
| 2 | The Brothers That Just Do Gutters | https://pensacola.brothersgutters.com/?utm_source=GMB&utm_medium=organic?utm_source=GMB&utm_medium=organic | 11020939739607606635 | 540 E Fairfield Dr Suite A02, Pensacola, FL 32503 |
| 3 | Pensacola Seamless Gutters | http://pensacolaseamlessgutters.com/ | 14844947346199191292 | 4771 Bayou Blvd, Pensacola, FL 32503 |

入选 3 家；完整排除记录及快照引用见 `seamless-gutters-pensacola/result.json`。

## Volcanoes Hiking Guides

GBP 状态：`confirmed_by_reviewer`。

| 名称 | 官网 | GBP | 电话 | 地址 |
| --- | --- | --- | --- | --- |
| Volcanoes Hiking Guides | https://www.volcanoeshikingguides.com/ | https://www.google.com/maps?cid=16906567364961418758 | (808) 491-9022 | 1515-55 11th Ave, Keaau, HI 96749 |

### 按裁定选出的 Q1 maps 竞品

| 顺位 | 商家 | 官网 | CID | 地址 |
| --- | --- | --- | --- | --- |
| 2 | Big Island Hilo Tours | https://bigislandhilotours.com/ | 13421160305445031640 | 301 Kamehameha Ave, Hilo, HI 96720 |
| 4 | C Big Island Tours | http://www.cbigisland.com/ | 18034467433998025927 | 71 Banyan Dr, Hilo, HI 96720 |

入选 2 家；完整排除记录及快照引用见 `volcanoes-hiking-guides/result.json`。

## Nova Service

GBP 状态：`not_requeried_existing_frozen_report`。

### 按裁定选出的 Q1 maps 竞品

| 顺位 | 商家 | 官网 | CID | 地址 |
| --- | --- | --- | --- | --- |
| 1 | PR Plumbing, Heating & Air Conditioning Inc. | https://www.prplumbingfresno.com/ | 9308775580876614598 | 1504 N Maple Ave, Fresno, CA 93703 |
| 5 | Low Cost Plumbing Services | http://www.low-costplumbingservices.com/ | 2344010983889749812 | 2793 N Sunnyside Ave Ste 104, Fresno, CA 93727 |
| 6 | Fresno Plumbing & Heating Inc | https://www.fresnoplumbinginc.com/ | 12978353145315488362 | 2585 N Larkin Ave, Fresno, CA 93727 |

入选 3 家；完整排除记录及快照引用见 `nova-service/result.json`。
