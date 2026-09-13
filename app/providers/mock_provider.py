from app.providers.base import CompanyProvider


class MockCompanyProvider(CompanyProvider):

    def get_company(self, inn: str):

        test_companies = {
            "7707083893": {
                "inn": "7707083893",
                "ogrn": "1027700132195",
                "name": "ПАО СБЕРБАНК",
                "status": "active",
                "risk_score": 10,
            },

            "7704217370": {
                "inn": "7704217370",
                "ogrn": "1027700229193",
                "name": "ПАО ЯНДЕКС",
                "status": "active",
                "risk_score": 5,
            }
        }

        return test_companies.get(inn)