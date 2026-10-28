# FlightWatch：伦敦 ⇄ 上海 机票监控

每 2 小时自动查一次 Google Flights 的价格。商务舱最低价下降时推送到手机，同时更新一个网页看板。程序完全在 GitHub 上运行（GitHub Actions），不需要自己的服务器，也不需要一直开着电脑。

**查什么**

- 伦敦希思罗 → 上海（浦东或虹桥），去程 12/11–12/16，回程 12/27–12/30，24 种日期组合全部比较
- 每一程（含转机）不超过 17 小时
- 长途段必须是商务舱或超级经济舱；国内短途转机段（5 小时以内）可以是经济舱
- 商务舱优先：看板大字、走势图和降价提醒都以「来回都是商务舱」为准；含超经的组合只在更便宜时作为次要信息出现
- 单程拼接和往返票都会比较

---

## 设置步骤（约 15 分钟，全部在网页上完成）

### 1. 注册 SerpApi，拿到 key

1. 打开 https://serpapi.com/users/sign_up 注册，选 **Free Plan**（每月 250 次，不用绑卡）
2. 打开 https://serpapi.com/manage-api-key ，复制 **Your Private API Key**

### 2. 手机安装 ntfy

1. App Store / Google Play 搜索 **ntfy** 并安装，允许通知
2. 点 **+**，Topic name 填你自己的主题名（别写进仓库，仓库是公开的），点 **Subscribe**

### 3. 在 GitHub 上建仓库并上传文件

1. 打开 https://github.com/new ，Repository name 填 `flightwatch`，选 **Public**，点 **Create repository**
2. 点页面上的 **uploading an existing file**
3. 在 Finder 里打开解压后的 `flightwatch` 文件夹，按 **Command + Shift + .** 显示隐藏文件（会多出一个 `.github` 文件夹）
4. 全选文件夹里的所有内容（包括 `.github`），拖进网页，点 **Commit changes**
5. 回到仓库首页，确认能看到 `.github` 文件夹和 `app.py`、`index.html` 等文件

> 如果 `.github` 没传上去：点 **Add file → Create new file**，文件名填 `.github/workflows/flightwatch.yml`，把本地同名文件的内容复制进去，点 **Commit changes**。

### 4. 填两个密钥

仓库页面 → **Settings** → 左边 **Secrets and variables** → **Actions** → **New repository secret**，添加两个：

| Name | Secret |
|---|---|
| `SERPAPI_KEY` | 第 1 步复制的 key |
| `NTFY_TOPIC` | 手机 ntfy 里订阅的同一个主题名 |

密钥是加密保存的，公开仓库别人也看不到。

### 5. 打开网页看板功能

仓库页面 → **Settings** → 左边 **Pages** → **Build and deployment** 下的 **Source** 选 **GitHub Actions**。

### 6. 第一次运行

1. 仓库页面 → **Actions**（如果提示要启用 workflows，点同意）
2. 左边点 **FlightWatch** → 右边 **Run workflow** → 绿色的 **Run workflow**
3. 第一次会把 32 个查询全部跑一遍，大约 5–15 分钟
4. 跑完后手机会收到「✈️ 监控已开始」
5. 打开看板：`https://livisliving.github.io/flightwatch/`（也可以在仓库 Settings → Pages 页面顶部找到这个地址）

之后每 2 小时会自动运行一次，不用再管。

---

## 日常使用

| 想做什么 | 怎么做 |
|---|---|
| 看价格 | 打开 `https://livisliving.github.io/flightwatch/` |
| 改日期、目标价等设置 | 在 GitHub 上打开 `.github/workflows/flightwatch.yml`，点铅笔图标，改 `env:` 下面的值，**Commit changes**。下次运行生效 |
| 只要商务舱（省一半额度） | 把 `CLASSES` 改成 `"business"` |
| 设心理价位 | 把 `TARGET_PRICE` 改成比如 `"2500"`，低于它会发高优先级提醒 |
| 马上查一次 | Actions → FlightWatch → Run workflow |
| 买到票后停止 | Actions → FlightWatch → 右上角 **···** → **Disable workflow**。12/16 之后也会自动停止查询 |

**提醒类型**

| 提醒 | 什么时候 | 响铃 |
|---|---|---|
| 🎯 商务舱低于目标价 | 第一次低于 `TARGET_PRICE` | 会（最高优先级） |
| 📉 商务舱降价 | 商务舱最低价降了 £20 以上 | 会 |
| 超经组合降价 | 含超经的组合降价，而且比商务舱便宜 | 不会 |
| ☀️ 今日商务舱最低 | 每天早上 8 点后第一次运行 | 不会 |

**额度**：免费版每月 250 次。程序会读取剩余次数，平均分配到截止日期或额度刷新日之前（哪个早按哪个），大约每天 8–14 次，当前便宜的日期查得更勤。升级 SerpApi 到 $25/月 会自动加快。

---

## 出问题怎么办

| 你看到的 | 怎么办 |
|---|---|
| Actions 里运行失败（红色 ×） | 点进去看哪一步是红的，截图发给 Claude |
| 看板底部有红字 `Invalid API key` | 重新复制 key，在 Settings → Secrets 里更新 `SERPAPI_KEY` |
| 红字里有 `run out of searches` | 本月额度用完了，等刷新或升级 |
| 看板打不开（404） | 确认第 5 步 Source 选的是 GitHub Actions，并且至少成功运行过一次 |
| 手机收不到提醒 | 对一下 ntfy 里的主题名和 `NTFY_TOPIC` 是否完全一样 |
| 日期表某格一直是「—」 | 那两天没有符合条件的航班，正常 |

> 看板是公开网页，知道地址的人都能看到价格和日期。API key 和 ntfy 主题保存在加密的 Secrets 里，不会公开；主题名不要写进仓库里的任何文件。

## 在自己电脑上预览（可选）

装了 Python 3 的话，在文件夹里运行 `FAKE_MODE=true python3 app.py`，再打开 http://localhost:8080 ，能看到用假数据生成的看板。
