package common

// Result summarises the outcome of a single importer run. It is returned by
// each importer after it finishes applying a source snapshot so that callers
// can record the counts in import_runs and surface them to operators.
type Result struct {
	RunID     int64
	Status    string
	Parsed    int
	Applied   int
	Warnings  int
	Conflicts int
	Unchanged bool
}
