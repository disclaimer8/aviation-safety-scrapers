"""Occurrence-date recovery for AHAC.

Every fixture below is a real fragment from an AHAC narrative on prod
(2026-08-06, 34 documents, all of them dateless). They are here in two groups:
the dates we must find, and the dates we must refuse. The second group is the
important one — a licence expiry and the Annex 13 boilerplate both read as
perfectly good Spanish dates, and "first date in the document" would take one
of them for most of this corpus.
"""
import pytest

from ahac_ingest.dates import recover_event_date


class TestTheLabelledForm:
    """Preliminary and provisional reports use an explicit field."""

    @pytest.mark.parametrize("text,expected", [
        ("ES, EL AGUAN, OLANCHITO DEPTO DE YORO.  Fecha de Accidente:  12-Enero-2025  "
         "Hora aproximada del Accidente:  23:35 UTC", "2025-01-12"),
        ("artamento de Gracias A Dios, Honduras.  Fecha de Accidente:  03-julio-2023  "
         "Hora aproximada del Accidente:  18:40 UTC", "2023-07-03"),
        ("DE VALENCIA, LA LIMA DEPTO DE CORTES.  Fecha de Accidente:  08-MARZO-2026  "
         "Hora aproximada del Accidente:  16:19 UTC", "2026-03-08"),
    ])
    def test_it_reads_fecha_de_accidente(self, text, expected):
        assert recover_event_date(text) == (expected, "label")

    def test_it_reads_the_incident_wording_too(self):
        text = "Fecha de Incidente: 05-Febrero-2024 Hora aproximada"
        assert recover_event_date(text) == ("2024-02-05", "label")

    def test_case_does_not_matter(self):
        assert recover_event_date("FECHA DEL ACCIDENTE: 1-DICIEMBRE-2021")[0] == "2021-12-01"


class TestTheTabularForm:
    """Older final reports lay the fields out with dot leaders."""

    def test_it_reads_a_dot_leader_row(self):
        text = (",971.6 CICLOS TOTALES……………………………….6,163 "
                "FECHA…………………………………………….23 DE ABRIL DEL 2010 "
                "HORA…………………………………………… 2245 UTC (APROXIMADAMENTE)")
        assert recover_event_date(text) == ("2010-04-23", "table")


class TestTheSynopsisSentence:
    """Final reports state it in prose, always with the time beside it.

    The date alone would be unsafe here — the same documents carry licence
    dates in identical wording. What makes this anchor sound is the pair: the
    synopsis says when the occurrence happened AND at what time, and no licence
    expiry in this corpus is followed by a clock time.
    """

    @pytest.mark.parametrize("text,expected", [
        ("zucarera La Grecia, en Marcovia, Departamento de Choluteca, el día 20 de "
         "octubre del año 2018, aproximadamente a las 1530 UTC.", "2018-10-20"),
        ("teniendo una excursión de pista el día 22 de diciembre del 2020 "
         "aproximadamente a las 5:55 hora local, 2355 UTC", "2020-12-22"),
        ("de aterrizaje, el 22 de mayo del año 2018, aproximadamente a las 17:15 UTC.",
         "2018-05-22"),
        ("investigación de este accidente ocurrido el día 23 de noviembre del 2017.",
         None),  # no time beside it — refused, see the test below
    ])
    def test_it_reads_the_synopsis(self, text, expected):
        got, basis = recover_event_date(text)
        assert got == expected
        assert basis == ("synopsis" if expected else None)

    def test_del_presente_ano_is_still_a_year(self):
        text = ("Ramón Villeda Morales, el día 10 de enero del presente año 2019, "
                "aproximadamente a las 1509UTC.")
        assert recover_event_date(text) == ("2019-01-10", "synopsis")

    def test_a_bare_date_with_no_time_is_not_taken(self):
        # Deliberate. Dropping the time requirement would pick up licence and
        # certificate dates worded exactly the same way.
        text = "El manual fue aprobado el día 14 de marzo del año 2019 por la autoridad."
        assert recover_event_date(text) == (None, None)


class TestTheDatesItMustRefuse:
    """Real text from the same documents. Each one is a wrong answer."""

    def test_the_annex_13_boilerplate_is_not_a_date(self):
        # This paragraph appears in most AHAC final reports and contains the
        # word "fecha" with no date at all — but a loose anchor would run on
        # and pick up whatever number came next.
        text = ("de los 30 días contados a partir de la fecha en que ocurrió el "
                "accidente, está clasificada por la OACI como lesión mortal. "
                "Nota 2. — Una aeronave se considera desaparecida el 14 de Marzo del 2019")
        assert recover_event_date(text) == (None, None)

    def test_a_licence_expiry_is_not_the_occurrence(self):
        text = ("Clase I se emitió el 17/Enero/2025, con fecha de expiración "
                "30/Julio/2025. Su último recurrente de vuelo lo obtuvo en fecha "
                "03 abril del 2024.")
        assert recover_event_date(text) == (None, None)

    def test_a_certificate_expiry_in_words_is_not_it_either(self):
        text = ("e de Línea Aérea Avión ATP No 3868, con fecha de expiración del "
                "30 de Diciembre del 2019 con habilitaciones Mono Motores")
        assert recover_event_date(text) == (None, None)

    def test_an_aircraft_build_year_is_not_a_date(self):
        text = "Modelo:  S2R-T34  Número de Serie:  T34-300  Año de Fabricación:  2009"
        assert recover_event_date(text) == (None, None)


class TestItRefusesRatherThanGuesses:
    def test_an_impossible_day_is_refused(self):
        assert recover_event_date("Fecha de Accidente: 31-Febrero-2025") == (None, None)

    def test_an_unknown_month_word_is_refused(self):
        assert recover_event_date("Fecha de Accidente: 12-Smarch-2025") == (None, None)

    def test_a_future_date_is_refused(self):
        # A typo in the year is common and yields a date that cannot have
        # happened. Better dateless than wrong.
        assert recover_event_date("Fecha de Accidente: 12-Enero-2099") == (None, None)

    def test_empty_in_nothing_out(self):
        assert recover_event_date("") == (None, None)
        assert recover_event_date(None) == (None, None)

    def test_a_document_with_no_date_field_stays_dateless(self):
        text = "INFORME FINAL ACCID DE LA AERONAVE HR-AZD Matricula: HR-AZD Marca: THRUSH"
        assert recover_event_date(text) == (None, None)


class TestTheLabelWinsOverTheTable:
    def test_when_both_are_present_the_explicit_field_is_used(self):
        text = ("FECHA…………………….01 DE ENERO DEL 2020 "
                "Fecha de Accidente:  12-Enero-2025")
        assert recover_event_date(text) == ("2025-01-12", "label")
