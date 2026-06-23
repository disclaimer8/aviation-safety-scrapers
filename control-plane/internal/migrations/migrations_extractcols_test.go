package migrations

import (
	"context"
	"database/sql"
	"testing"
)

func cols(t *testing.T, db *sql.DB, table string) map[string]bool {
	t.Helper()
	rows, err := db.Query("SELECT name FROM pragma_table_info(?)", table)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	got := map[string]bool{}
	for rows.Next() {
		var n string
		if err := rows.Scan(&n); err != nil {
			t.Fatal(err)
		}
		got[n] = true
	}
	return got
}

func TestMigration009AddsExtractColumns(t *testing.T) {
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := Apply(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	want := []string{"download_status", "local_file_path", "digest", "ocr_text_path",
		"extraction_status", "extraction_error", "extraction_attempts", "event_id"}
	for _, table := range []string{"staged_regional_documents", "staged_foreign_documents"} {
		got := cols(t, db, table)
		for _, c := range want {
			if !got[c] {
				t.Errorf("%s missing column %s", table, c)
			}
		}
	}
}
