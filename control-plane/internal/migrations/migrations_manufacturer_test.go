package migrations

import (
	"context"
	"database/sql"
	"testing"
	_ "modernc.org/sqlite"
)

func TestMigration009ManufacturerTable(t *testing.T) {
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil { t.Fatal(err) }
	defer db.Close()
	if err := Apply(context.Background(), db); err != nil { t.Fatal(err) }
	rows, err := db.Query("SELECT name FROM pragma_table_info('staged_manufacturer_documents')")
	if err != nil { t.Fatal(err) }
	defer rows.Close()
	got := map[string]bool{}
	for rows.Next() { var n string; if err := rows.Scan(&n); err != nil { t.Fatal(err) }; got[n] = true }
	for _, c := range []string{"manufacturer","publication","issue_ref","title","publication_date",
		"original_url","report_url","download_status","extraction_status","event_id"} {
		if !got[c] { t.Errorf("missing column %s", c) }
	}
}
