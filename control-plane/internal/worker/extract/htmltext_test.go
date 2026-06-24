package extract

import (
	"strings"
	"testing"
)

func TestHtmlToText(t *testing.T) {
	tests := []struct {
		name      string
		input     string
		wantSubs  []string // substrings that must be present
		wantNot   []string // substrings that must not be present
		wantEmpty bool     // expect "" result
	}{
		{
			name: "strips script blocks",
			input: `<html><body>
				<script type="text/javascript">var x = 1; alert("boom");</script>
				<p>Incident date: 2026-05-19</p>
			</body></html>`,
			wantSubs: []string{"Incident date: 2026-05-19"},
			wantNot:  []string{"alert", "var x"},
		},
		{
			name: "strips style blocks",
			input: `<html><head><style>body { color: red; }</style></head>
				<body><p>Aircraft: AN-2</p></body></html>`,
			wantSubs: []string{"Aircraft: AN-2"},
			wantNot:  []string{"color: red"},
		},
		{
			name: "strips all html tags",
			input: `<div class="report"><h1>Final Report</h1>
				<p>Registration: <b>RA-40440</b></p>
				<table><tr><td>Operator:</td><td>Aviaprom</td></tr></table>
			</div>`,
			wantSubs: []string{"Final Report", "Registration:", "RA-40440", "Operator:", "Aviaprom"},
			wantNot:  []string{"<div", "<h1>", "<b>", "<table>"},
		},
		{
			name:     "unescapes html entities",
			input:    `<p>Damage: &lt;total&gt; &amp; operator: &quot;Test&quot;</p>`,
			wantSubs: []string{"Damage: <total>", "& operator:", `"Test"`},
			wantNot:  []string{"&lt;", "&gt;", "&amp;", "&quot;"},
		},
		{
			name: "collapses whitespace",
			input: `<p>   Fatalities:   157   </p>
				<p>
				   Location:   Bishoftu
				</p>`,
			wantSubs: []string{"Fatalities: 157", "Location: Bishoftu"},
		},
		{
			name:      "empty input returns empty",
			input:     "",
			wantEmpty: true,
		},
		{
			name:      "whitespace-only input returns empty",
			input:     "   \n\t  ",
			wantEmpty: true,
		},
		{
			name:      "tags-only html returns empty",
			input:     `<html><head></head><body>   </body></html>`,
			wantEmpty: true,
		},
		{
			name: "iac-style report page with all key fields",
			input: `<html><head>
				<title>Отчёт о расследовании</title>
				<style>.nav { display: none; }</style>
				<script>analytics();</script>
			</head><body>
				<h1>Расследование авиационного происшествия</h1>
				<table>
					<tr><td>Дата:</td><td>19.05.2026</td></tr>
					<tr><td>Воздушное судно:</td><td>Ан-2</td></tr>
					<tr><td>Регистрация:</td><td>RA-40440</td></tr>
					<tr><td>Оператор:</td><td>ООО &quot;Авиапром&quot;</td></tr>
					<tr><td>Погибших:</td><td>0</td></tr>
					<tr><td>Повреждения ВС:</td><td>незначительные</td></tr>
				</table>
				<p>Самолёт выполнял авиационные работы по сельхозобработке полей.</p>
			</body></html>`,
			wantSubs: []string{
				"19.05.2026",
				"Ан-2",
				"RA-40440",
				`"Авиапром"`,
				"Самолёт выполнял",
			},
			wantNot: []string{
				"analytics",
				"display: none",
				"<td>",
				"&quot;",
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := htmlToText([]byte(tc.input))
			if tc.wantEmpty {
				if got != "" {
					t.Fatalf("expected empty string, got %q", got)
				}
				return
			}
			for _, sub := range tc.wantSubs {
				if !strings.Contains(got, sub) {
					t.Errorf("result missing %q\nfull result: %q", sub, got)
				}
			}
			for _, sub := range tc.wantNot {
				if strings.Contains(got, sub) {
					t.Errorf("result must not contain %q\nfull result: %q", sub, got)
				}
			}
		})
	}
}
