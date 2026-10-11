# 第三方資源聲明

以下聲明適用於各第三方資源。各項授權不授予對整個 HouKongIntellect 專案的統一授權，也不表示原作者對本專案提供背書。

## 隨倉庫提供的前端資源

| 本機檔案 | 來源與用途 | 授權文件 |
| --- | --- | --- |
| `marked.js` | [Marked](https://github.com/markedjs/marked)，Markdown 解析。Copyright 2018+ MarkedJS；2011–2018 Christopher Jeffrey。 | [上游授權](https://github.com/markedjs/marked/blob/master/LICENSE) / [本機副本](licenses/marked-LICENSE.txt)。Marked 採 MIT，上游文件另保留 Markdown 來源聲明。 |
| `tailwindcss.js` | [Tailwind CSS](https://github.com/tailwindlabs/tailwindcss)，瀏覽器樣式產生。Copyright Tailwind Labs, Inc. | MIT：[上游授權](https://github.com/tailwindlabs/tailwindcss/blob/main/LICENSE) / [本機副本](licenses/tailwindcss-LICENSE.txt)。 |
| `pdf.min.js`、`pdf.worker.min.js` | [Mozilla PDF.js 3.11.174](https://github.com/mozilla/pdf.js/tree/v3.11.174)，PDF 預覽。Copyright 2023 Mozilla Foundation。 | Apache-2.0：[上游授權](https://github.com/mozilla/pdf.js/blob/v3.11.174/LICENSE) / [本機副本](licenses/pdfjs-LICENSE.txt)。保留原始檔頭聲明。 |
| `font-awesome.css` | [Font Awesome 4.7.0](https://github.com/FortAwesome/Font-Awesome/tree/v4.7.0)，Dave Gandy 提供的圖示樣式。 | MIT：[官方授權說明](https://fontawesome.com/v4/license/) / [本機文字](licenses/fontawesome-MIT.txt)。保留 CSS 檔頭聲明。 |
| `fonts/fontawesome-webfont.woff2` | Font Awesome 4.7.0 的未修改圖示字型，保留原字型中繼資料。 | SIL OFL-1.1：[官方授權說明](https://fontawesome.com/v4/license/) / [本機文字](licenses/fontawesome-OFL.txt)。 |

Marked 和 Tailwind 的現有瀏覽器檔案沒有可核實的完整套件版本標記，本文件不以其他上游版本代替實際版本。更新這些資源時，應記錄下載來源、版本及對應的授權文件。

### Tailwind 瀏覽器檔案中的第三方程式

現有 bundle 保留了 fill-range、is-number、to-regex-range 及 cssesc 的來源標記；另可辨識出 PostCSS 8.4.49 的版本標記。相關聲明與授權副本如下：

| 程式 | 上游來源 | 授權副本 |
| --- | --- | --- |
| fill-range | [jonschlinkert/fill-range](https://github.com/jonschlinkert/fill-range)，Copyright 2014–present Jon Schlinkert。 | [MIT](licenses/fill-range-LICENSE.txt) |
| is-number | [jonschlinkert/is-number](https://github.com/jonschlinkert/is-number)，Copyright 2014–present Jon Schlinkert。 | [MIT](licenses/is-number-LICENSE.txt) |
| to-regex-range | [micromatch/to-regex-range](https://github.com/micromatch/to-regex-range)，Copyright 2015–present Jon Schlinkert。 | [MIT](licenses/to-regex-range-LICENSE.txt) |
| cssesc 3.0.0 | [mathiasbynens/cssesc](https://github.com/mathiasbynens/cssesc/tree/v3.0.0)，Copyright Mathias Bynens。 | [MIT](licenses/cssesc-LICENSE.txt) |
| PostCSS 8.4.49 | [postcss/postcss](https://github.com/postcss/postcss/tree/8.4.49)，Copyright 2013 Andrey Sitnik。 | [MIT](licenses/postcss-LICENSE.txt) |

原始 bundle 中的既有授權註解保留不變。此表記錄已辨識資源，不是完整的依賴組成清單；重新建置或更新 bundle 時應一併保留其全部依賴授權。

## 可選遠端字型

前端可從 Google Fonts 載入 [Inter](https://github.com/rsms/inter)。Inter 由 The Inter Project Authors 提供，採 SIL OFL-1.1；[上游授權](https://github.com/rsms/inter/blob/master/LICENSE.txt)及[本機副本](licenses/inter-LICENSE.txt)供參考。倉庫不內附 Inter 字型檔，無法連線時使用系統字型。

## Python 套件與模型

核心、開發及本地模型依賴分別由 `requirements.txt`、`requirements-dev.txt` 和 `requirements-local.txt` 安裝，套件本身不隨倉庫重新打包。各套件仍適用自己的授權及通知。

可選本地模型預設為 [shibing624/text2vec-base-chinese](https://huggingface.co/shibing624/text2vec-base-chinese)，由部署者另行下載。使用前請閱讀該版本的模型卡與授權；模型權重不包含在本倉庫內。
