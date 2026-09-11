const X = require('xlsx');
const fs = require('fs');
const files = ['fy2020_q1_uas_sightings.xlsx', 'fy23_q4.xlsx', 'fy25_q3.xlsx', 'fy26_q2_uas_sightings.xlsx'];
for (const f of files) {
  if (!fs.existsSync(f)) { console.log(f, 'MISSING'); continue; }
  const wb = X.readFile(f, { cellDates: true });
  const sh = wb.Sheets[wb.SheetNames[0]];
  const aoa = X.utils.sheet_to_json(sh, { header: 1, blankrows: false });
  // find header row = first with >=3 string cells
  let hi = aoa.findIndex(r => r.filter(c => typeof c === 'string' && c.length > 1).length >= 3);
  if (hi < 0) hi = 0;
  console.log(`\n${f}: sheets=${JSON.stringify(wb.SheetNames)} rows=${aoa.length} headerRow=${hi}`);
  console.log('  cols=' + JSON.stringify(aoa[hi]));
  console.log('  sample=' + JSON.stringify(aoa[hi+1]));
}
