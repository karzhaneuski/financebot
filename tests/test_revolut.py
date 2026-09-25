"""Tests for the Revolut consolidated statement parser."""

from bot.parsers.revolut import is_revolut_statement, parse_revolut_csv

# Small synthetic excerpt mirroring the real "Сводные данные" (consolidated
# statement) export shape:
# an account-summary block (balances only, must be skipped) followed by a
# transaction-statement block with two purchases and one currency-exchange
# row (must be skipped).
SAMPLE_CSV = (
    '"Личный счет (EUR)",,,,,,,,,,,,\n'
    ',,,,,,,,,,,,\n'
    '"Реквизиты текущего счета",,,,,,,,,,,,\n'
    '"Номер счета (IBAN)",LT383250036948901146,"Дата открытия","20 февр. 2026 г.",,,,,,,,,\n'
    '"Сумма внесения",,,,,,,,,,,,\n'
    ',,"Начальный баланс","0,00€","0,00 PLN",,,,,,,,\n'
    ',,"Конечный остоток","36,25€","156,56 PLN",,,,,,,,\n'
    ',,,,,,,,,,,,\n'
    '---------,,,,,,,,,,,,\n'
    ',,,,,,,,,,,,\n'
    '"Личный счет (EUR)",,,,,,,,,,,,\n'
    ',,,,,,,,,,,,\n'
    '"Выписка по операциям",,,,,,,,,,,,\n'
    'Дата,Описание,Категория,"Поступления / Списания","Поступления / Списания",Баланс,Баланс,'
    '"Удержанный налог","Удержанный налог","Прочие налоги","Прочие налоги",Комиссии,Комиссии\n'
    '"22 февр. 2026 г.","Обменено на EUR","Обмен валюты","22,50€","95,04 PLN","22,50€","95,04 PLN",'
    '"0,00€","0,00 PLN","0,00€","0,00 PLN","0,00€","0,00 PLN"\n'
    '"23 февр. 2026 г.",MobiMatter,"Торговая точка","-22,50€","-95,04 PLN","0,00€","0,00 PLN",'
    '"0,00€","0,00 PLN","0,00€","0,00 PLN","0,00€","0,00 PLN"\n'
    '"28 мар. 2026 г.",Celestara,"Торговая точка","-6,99€","-29,97 PLN","3,01€","12,91 PLN",'
    '"0,00€","0,00 PLN","0,00€","0,00 PLN","0,00€","0,00 PLN"\n'
    'Итого,,,"36,25€","157,05 PLN",,,"0,00€","0,00 PLN","0,00€","0,00 PLN","0,00€","0,00 PLN"\n'
    ',,,,,,,,,,,,\n'
    '---------,,,,,,,,,,,,\n'
)

PLN_ACCOUNT_CSV = (
    '"Личный счет (PLN)",,,,,,,,,,,,\n'
    ',,,,,,,,,,,,\n'
    '"Выписка по операциям",,,,,,,,,,,,\n'
    'Дата,Описание,Категория,"Поступления / Списания",Баланс,"Удержанный налог","Прочие налоги",Комиссии,,,,,\n'
    '"5 мар. 2026 г.",Kaufland,"Торговая точка","-4,99 PLN","17,61 PLN","0,00 PLN","0,00 PLN","0,00 PLN",,,,,\n'
    '"6 мар. 2026 г.","Пополнение счета Apple Pay",Пополнение,"100,00 PLN","117,61 PLN","0,00 PLN","0,00 PLN","0,00 PLN",,,,,\n'
    'Итого,,,"17,61 PLN",,"0,00 PLN","0,00 PLN","0,00 PLN",,,,,\n'
    ',,,,,,,,,,,,\n'
    '---------,,,,,,,,,,,,\n'
)

HEADER_TEXT = '"Revolut Bank UAB"\n"Выписка по операциям"\n'


class TestIsRevolutStatement:
    def test_detects_real_markers(self):
        assert is_revolut_statement(HEADER_TEXT + SAMPLE_CSV) is True

    def test_rejects_unrelated_text(self):
        assert is_revolut_statement("Erste Bank Polska\nHISTORIA RACHUNKU") is False

    def test_requires_both_markers(self):
        assert is_revolut_statement("Revolut Bank UAB only") is False
        assert is_revolut_statement("Выписка по операциям only") is False


class TestParseRevolutCsv:
    def test_only_purchase_rows_imported(self):
        csv_bytes = (HEADER_TEXT + SAMPLE_CSV).encode("utf-8")
        transactions = parse_revolut_csv(csv_bytes)

        # Only the two "Торговая точка" (merchant) rows should survive — the
        # account-summary block and the "Обмен валюты" (currency exchange) row
        # must both be skipped.
        assert len(transactions) == 2
        descriptions = {t["description"] for t in transactions}
        assert descriptions == {"MobiMatter", "Celestara"}

    def test_purchase_fields_are_correct(self):
        csv_bytes = (HEADER_TEXT + SAMPLE_CSV).encode("utf-8")
        transactions = parse_revolut_csv(csv_bytes)
        tx = next(t for t in transactions if t["description"] == "MobiMatter")

        assert tx["date"] == "2026-02-23"
        assert tx["currency"] == "EUR"
        assert tx["amount"] == 22.50
        assert tx["total_pln"] == 95.04
        assert tx["type"] == "purchase"

    def test_account_summary_block_produces_no_transactions(self):
        # Isolate just the summary block (before the divider) to confirm it
        # alone yields nothing even though it mentions the account currency.
        summary_only = SAMPLE_CSV.split("---------")[0]
        csv_bytes = (HEADER_TEXT + summary_only).encode("utf-8")
        assert parse_revolut_csv(csv_bytes) == []

    def test_home_currency_account_uses_amount_as_pln(self):
        """PLN-native accounts have no duplicate PLN column — amount IS the PLN total."""
        csv_bytes = (HEADER_TEXT + PLN_ACCOUNT_CSV).encode("utf-8")
        transactions = parse_revolut_csv(csv_bytes)

        assert len(transactions) == 1
        tx = transactions[0]
        assert tx["description"] == "Kaufland"
        assert tx["currency"] == "PLN"
        assert tx["amount"] == 4.99
        assert tx["total_pln"] == 4.99

    def test_top_up_rows_are_skipped(self):
        csv_bytes = (HEADER_TEXT + PLN_ACCOUNT_CSV).encode("utf-8")
        transactions = parse_revolut_csv(csv_bytes)
        assert all(t["description"] != "Пополнение счета Apple Pay" for t in transactions)
