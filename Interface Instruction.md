# 山东农业大学教务系统接口说明

本文记录本项目已经确认并使用的教务系统接口。接口均为登录后的同源请求，实际响应可能随教务系统升级发生变化。

基础地址：

```text
https://jw.sdau.edu.cn
```

## 1. 登录

### `POST /xk/LoginToXk`

登录页会提供动态登录参数，提交表单后建立会话 Cookie。

主要表单字段：

| 参数 | 说明 |
|---|---|
| `loginMethod` | 固定为 `LoginToXk` |
| `userlanguage` | 语言标识，通常为 `0` |
| `userAccount` | 学号 |
| `userPassword` | 页面流程中通常为空 |
| `encoded` | 按登录页动态参数生成的编码凭据 |

后续接口必须复用登录会话。

## 2. 查询已选课程

### `GET /xkgl/loadXsxkjgList`

示例：

```text
/xkgl/loadXsxkjgList?lx=xkrz&type=list&pageNum=1&pageSize=200&xnxqid=2026-2027-1
```

参数：

| 参数 | 说明 |
|---|---|
| `lx` | `xkrz`，查询选课结果 |
| `type` | `list` |
| `pageNum` | 页码 |
| `pageSize` | 每页数量 |
| `xnxqid` | 学年学期，如 `2026-2027-1` |

响应根字段通常包括 `code`、`msg`、`data`、`count`。

课程对象常用字段：

| 字段 | 说明 |
|---|---|
| `kc_mc` | 课程名称 |
| `kch` | 课程代码 |
| `xm` | 教师 |
| `zxs` | 总学时 |
| `xf` | 学分 |
| `kclb_mc` | 课程类别 |
| `sksj` | 上课时间，可能包含周次 |
| `skdd` | 上课地点 |
| `jx02id` | 课程内部 ID |
| `jx0501id` | 教学安排 ID |

## 3. 查询个人课表周次

### `GET /xskb/xskb_list.do`

示例：

```text
/xskb/xskb_list.do?viweType=0&xnxq01id=2026-2027-1
```

参数：

| 参数 | 说明 |
|---|---|
| `viweType` | 页面视图类型，当前使用 `0` |
| `xnxq01id` | 学年学期 |

该页面中的课程文本通常包含课程名、教师、周次、节次、地点和课程编号，用于补充精确周次。

## 4. 查询选课轮次

### `GET /xsxk/xklc_list_data`

示例：

```text
/xsxk/xklc_list_data?pageNum=1&pageSize=200
```

参数：

| 参数 | 说明 |
|---|---|
| `pageNum` | 页码 |
| `pageSize` | 每页数量 |

轮次对象常用字段：

| 字段 | 说明 |
|---|---|
| `jx0502zbid` | 选课轮次唯一 ID |
| `xnxq01id` | 学年学期 |
| `xklc_mc` | 轮次名称 |
| `xkzt` | 轮次状态；`1` 表示开放 |
| `xkkssj` | 选课开始时间，如 `2026-08-16 10:00` |
| `xkjzsj` | 选课结束时间，如 `2026-08-21 10:00` |
| `xksj` | 开始和结束时间的展示文本 |
| `xqmc` | 学期名称 |
| `yxzt` | 轮次运行状态 |

同一学期有多个轮次时，程序选择 `xkzt=1` 且当前时间处于 `xkkssj` 到 `xkjzsj` 之间的唯一轮次。

## 5. 进入选课轮次

### `GET /xsxk/newXsxkzx`

示例：

```text
/xsxk/newXsxkzx?jx0502zbid=43D181E0052C4CA49CE19A1998FF8C59&isallsc=
```

参数：

| 参数 | 说明 |
|---|---|
| `jx0502zbid` | 第 4 节查询到的轮次 ID |
| `isallsc` | 页面进入参数，当前使用空值 |

页面通常包含以下子页面：

```text
/xsxk/selectNum?jx0502zbid=...
/xsxk/selectBottom?jx0502zbid=...&sfylxkstr=
```

`selectBottom` 是课程类别导航，不是最终提交接口。

## 6. 公选课页面

当前项目只使用公选课页面。

### 页面：`GET /xsxkkc/getGgxxk`

用于显示公选课筛选条件和课程列表。

### 列表：`POST /xsxkkc/xsxkGgxxkxk`

示例 URL：

```text
/xsxkkc/xsxkGgxxkxk?kcxx=&skls=&skxq=&skjc=&endJc=&sfym=false&sfct=true&szjylb=&sfxx=true&skfs=&kctype=
```

查询参数：

| 参数 | 说明 |
|---|---|
| `kcxx` | 课程编号或名称筛选 |
| `skls` | 教师筛选 |
| `skxq` | 星期筛选 |
| `skjc` | 起始节次 |
| `endJc` | 结束节次 |
| `sfym` | 是否过滤已满课程 |
| `sfct` | 是否过滤时间冲突课程 |
| `szjylb` | 通选课类别 |
| `sfxx` | 是否过滤限选课程 |
| `skfs` | 授课方式 |
| `kctype` | 课程类型 |

请求体还包含列表分页参数，常见字段为：

| 参数 | 说明 |
|---|---|
| `sEcho` | 列表请求序号 |
| `iDisplayStart` | 起始记录位置 |
| `iDisplayLength` | 返回数量 |

响应根字段：

| 字段 | 说明 |
|---|---|
| `aaData` | 课程数组 |
| `sEcho` | 请求序号 |
| `iTotalRecords` | 总记录数 |
| `iTotalDisplayRecords` | 过滤后的记录数 |
| `jx0502zbid` | 当前轮次 ID |
| `flag`、`flag1` | 页面业务状态标记 |

课程对象常用字段：

| 字段 | 说明 |
|---|---|
| `jx0404id` | 教学班唯一 ID，配置和提交的核心字段 |
| `jx02id` | 课程内部 ID，提交参数中的 `kcid` |
| `kch` | 课程代码 |
| `kcmc` | 课程名称 |
| `skls` | 教师 |
| `syrs` | 剩余容量展示值 |
| `pkrs` | 课程容量 |
| `xkrs` | 已选人数 |
| `cfbs` | 冲突标志，可能为 `null` |
| `xqid` | 校区或课程区域代码 |
| `xqmc` | 校区名称 |
| `sksj` | 上课时间 |
| `skdd` | 上课地点 |
| `xf` | 学分 |
| `zxs` | 总学时 |
| `xkzt` | 课程选课状态 |

## 7. 提交公选课

### `GET /xsxkkc/ggxxkxkOper`

已确认的成功请求示例：

```text
/xsxkkc/ggxxkxkOper?kcid=BK109011&cfbs=null&jx0404id=202620271006153&xkzy=&trjf=&sfsyjc=
```

参数：

| 参数 | 说明 |
|---|---|
| `kcid` | 课程内部 ID，通常取列表行的 `jx02id` |
| `cfbs` | 冲突标志；无值时使用 `null` |
| `jx0404id` | 教学班唯一 ID |
| `xkzy` | 选课专业或专业方向参数，通常为空 |
| `trjf` | 退、让或积分相关参数，通常为空 |
| `sfsyjc` | 是否使用借出或加课参数，通常为空 |

成功响应示例：

```json
{
  "success": true,
  "message": "选课成功",
  "jfViewStr": ""
}
```

程序根据 `success`、`message` 和错误信息判断结果。响应不明确时不能自动重复提交。

## 8. 未纳入当前版本的接口

以下页面属于其他课程类别，当前公选课抢课流程不会调用：

```text
/xsxkkc/getBxxkxx   必修选课
```

其他类别可能有不同的列表和提交端点，不能直接套用公选课参数。
