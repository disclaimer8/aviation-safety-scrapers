package manufacturer

// ManufacturerRecord holds the metadata for a single Airbus Safety First
// magazine issue discovered from the listing page.
type ManufacturerRecord struct {
	// IssueRef is the stable identifier: the issue number (e.g. "41") for
	// numbered issues, or a short slug for special editions
	// (e.g. "special_edition_-_control_your_speed").
	IssueRef string

	// Title is a human-readable name synthesised from the filename, since the
	// listing page does not expose a text title inside the anchor.
	// Examples: "Safety First #41", "Safety First: Control Your Speed"
	Title string

	// PublicationDate is the issue's publication date in ISO yyyy-mm-dd form,
	// or "" when not extractable from the listing fixture.
	PublicationDate string

	// OriginalURL is the absolute URL of the issue's primary landing/download
	// page.  Because the listing links directly to the PDF, this equals ReportURL.
	OriginalURL string

	// ReportURL is the absolute URL of the PDF, or "" when not found.
	ReportURL string
}
