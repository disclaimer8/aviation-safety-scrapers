const X = require('xlsx');
for (const y of [2022, 2023, 2024]) {
  const wb = X.readFile(`./laser-${y}.xlsx`);
  console.log(`\n${y}: sheets=${JSON.stringify(wb.SheetNames)}`);
  const sh = wb.Sheets[wb.SheetNames[wb.SheetNames.length - 1]]; // last sheet often "All"
  const aoa = X.utils.sheet_to_json(sh, { header: 1, blankrows: false });
  console.log(`  last-sheet rows=${aoa.length}`);
  console.log(`  row0=${JSON.stringify(aoa[0])}`);
  console.log(`  row1=${JSON.stringify(aoa[1])}`);
}
