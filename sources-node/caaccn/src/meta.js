// caaccn metadata — the curated part of this source.
//
// This is not a scraper. The nine mainland-China reports (CAAC regional, MEM
// special-major, CAAC-central MU5735) were located by hand, their text
// extracted to docs/*.txt, and their metadata recorded here. All 官方文件, so
// PRC Copyright Law Art. 5 puts them outside copyright.
//
// Kept in its own module so the data and the narrative composition can be
// tested without building a database.
'use strict';
const fs = require('fs');
const path = require('path');

const DOCS_META = [
  { f: 'hd1', case_id: 'CAAC-HD-2026-0102', event_date: '2026-01-02', reg: null, aircraft: '运动类飞机 sport aircraft',
    operator: '浙江星空翔业通用航空', location: '湖州长兴县, China', report_type: '一般事故调查处理报告', fatal: null,
    url: 'https://hd.caac.gov.cn/HD_XXGK/HD_TZGG/202606/t20260612_231061.html',
    title: '浙江星空翔业通用航空"1·2"湖州长兴县运动类飞机单飞转场训练坠机一般事故调查处理报告' },
  { f: 'hd2', case_id: 'CAAC-HD-2025-0502', event_date: '2025-05-02', reg: null, aircraft: '直升机 helicopter',
    operator: '苏州诚翼通用航空', location: '苏州吴中区, China', report_type: '一般事故调查处理报告', fatal: null,
    url: 'https://hd.caac.gov.cn/HD_XXGK/HD_TZGG/202606/t20260612_231059.html',
    title: '苏州诚翼通用航空"5·2"苏州吴中区直升机空中失去动力坠落一般事故调查处理报告' },
  { f: 'zn', case_id: 'CAAC-ZN-2025-0911', event_date: '2025-09-11', reg: 'B-7515', aircraft: '直升机 helicopter',
    operator: '湖北宇航公务航空', location: '随州市, China', report_type: '一般事故调查处理情况通报', fatal: null,
    url: 'http://zn.caac.gov.cn/ZN_XXGK/ZN_ZWGG/202603/t20260310_230239.html',
    title: '湖北宇航公务航空有限公司B-7515号直升机在随州市坠地一般事故调查处理情况的通报' },
  { f: 'xj', case_id: 'CAAC-XJ-2025-0905', event_date: '2025-09-05', reg: null, aircraft: '通用航空器 general aviation aircraft',
    operator: '新疆亚心通用航空', location: 'Xinjiang, China', report_type: '较大事故调查处理情况通报', fatal: null,
    url: 'https://xj.caac.gov.cn/XJ_XXGK/XJ_TZGG/202512/t20251215_229405.html',
    title: '新疆亚心通用航空有限公司"9·5"失控坠地较大事故调查处理情况的通报' },
  { f: 'xn', case_id: 'CAAC-XN-2025-0620', event_date: '2025-06-20', reg: 'B-10LZ', aircraft: '通用航空器 general aviation aircraft',
    operator: '海南亚太通用航空', location: '那曲市比如县, China', report_type: '较大事故调查处理报告', fatal: null,
    url: 'http://xn.caac.gov.cn/XN_XXGK/XN_TZGG/202602/t20260213_230087.html',
    title: '海南亚太通用航空有限公司"6·20"B-10LZ号机在那曲市比如县夏曲镇空中失控/失速坠毁较大事故调查处理报告' },
  { f: 'memyichun', case_id: 'MEM-2010-0824', event_date: '2010-08-24', reg: 'B-3130', aircraft: 'Embraer ERJ-190',
    operator: '河南航空 Henan Airlines', location: '伊春 Yichun, China', report_type: '特别重大事故调查报告', fatal: 44,
    url: 'https://www.mem.gov.cn/gk/sgcc/tbzdsgdcbg/2012/201206/t20120629_245232.shtml',
    title: '河南航空有限公司黑龙江伊春"8·24"特别重大飞机坠毁事故调查报告' },
  { f: 'membaotou', case_id: 'MEM-2004-1121', event_date: '2004-11-21', reg: 'B-3072', aircraft: 'Bombardier CRJ-200',
    operator: '中国东方航空 China Eastern', location: '包头 Baotou, China', report_type: '特别重大事故处理结果', fatal: 55,
    url: 'https://www.mem.gov.cn/gk/sgcc/tbzdsgdcbg/2006/200612/t20061221_245272.shtml',
    title: '中国东方航空云南公司包头"11·21"特别重大空难事故基本情况及处理结果' },
  { f: 'mu5735prelim', case_id: 'CAAC-MU5735-PRELIM', event_date: '2022-03-21', reg: 'B-1791', aircraft: 'Boeing 737-800',
    operator: '东方航空云南 China Eastern Yunnan', location: '梧州 Wuzhou, Guangxi, China', report_type: '初步调查情况通报', fatal: 132,
    url: 'https://www.caac.gov.cn/XXGK/XXGK/TZTG/202204/t20220420_212895.html',
    title: '"3·21"东航MU5735航空器飞行事故调查初步报告情况通报' },
  { f: 'mu5735progress', case_id: 'CAAC-MU5735-PROGRESS', event_date: '2022-03-21', reg: 'B-1791', aircraft: 'Boeing 737-800',
    operator: '东方航空云南 China Eastern Yunnan', location: '梧州 Wuzhou, Guangxi, China', report_type: '调查进展情况通报', fatal: 132,
    url: 'https://www.caac.gov.cn/XXGK/XXGK/TZTG/202403/t20240320_223268.html',
    title: '"3·21"东航MU5735航空器飞行事故调查进展情况通报' },
];

// The report title is prepended when the extracted text does not already lead
// with it: some documents begin mid-contents-page, so the body would otherwise
// open on a table of contents rather than the event.
function composeNarrative(docsDir, m) {
  const txtPath = path.join(docsDir, `${m.f}.txt`);
  if (!fs.existsSync(txtPath)) return null;
  let narr = fs.readFileSync(txtPath, 'utf-8').replace(/\s+/g, ' ').trim();
  if (!narr.startsWith(m.title.slice(0, 10))) narr = `${m.title}。 ${narr}`;
  return narr;
}

module.exports = { DOCS_META, composeNarrative };
