const X = require('xlsx');
for (const y of [2021, 2025]) {
  const wb = X.readFile(`./laser-${y}.xlsx`);
  const sh = wb.Sheets[wb.SheetNames[0]];
  const rows = X.utils.sheet_to_json(sh);
  const cols = Object.keys(rows[0] || {});
  console.log(`${y}: sheets=${JSON.stringify(wb.SheetNames)} rows=${rows.length}`);
  console.log(`   cols=${JSON.stringify(cols)}`);
  console.log(`   sample=${JSON.stringify(rows[0])}`);
}
