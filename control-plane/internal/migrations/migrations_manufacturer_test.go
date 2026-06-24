package migrations

import (
	"context"
	"database/sql"
	"testing"
	_ "modernc.org/sqlite"
)

func TestMigration010ManufacturerTable(t *testing.T) {
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil { t.Fatal(err) }
	defer db.Close()
	if err := Apply(context.Background(), db); err != nil { t.Fatal(err) }
	rows, err := db.Query("SELECT name FROM pragma_table_info('staged_manufacturer_documents')")
	if err != nil { t.Fatal(err) }
	defer rows.Close()
	got := map[string]bool{}
	for rows.Next() { var n string; if err := rows.Scan(&n); err != nil { t.Fatal(err) }; got[n] = true }
	// Assert all required columns (base + download/extraction state columns)
	for _, c := range []string{"id","manufacturer","publication","issue_ref","title","publication_date",
		"original_url","report_url","mimetype","download_status","local_file_path","digest","ocr_text_path",
		"extraction_status","extraction_error","extraction_attempts","event_id","created_at"} {
		if !got[c] { t.Errorf("missing column %s", c) }
	}
}
