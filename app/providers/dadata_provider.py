import os

import httpx
from dotenv import load_dotenv

from app.providers.base import CompanyProvider


load_dotenv()


class DadataCompanyProvider(CompanyProvider):

    API_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"

    def __init__(self):
        self.api_key = os.getenv("DADATA_API_KEY")

        if not self.api_key:
            raise RuntimeError(
                "Не найден DADATA_API_KEY в файле .env"
            )

    def get_company(self, inn: str):
        headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        payload = {
            "query": inn,
            "count": 1,
        }

        response = httpx.post(
            self.API_URL,
            headers=headers,
            json=payload,
            timeout=20.0,
        )

        response.raise_for_status()

        data = response.json()

        suggestions = data.get("suggestions", [])

        if not suggestions:
            return None

        item = suggestions[0]
        company_data = item.get("data", {})

        state = company_data.get("state") or {}
        name_data = company_data.get("name") or {}
        management = company_data.get("management") or {}
        address_data = company_data.get("address") or {}

        return {
            "inn": company_data.get("inn"),
            "kpp": company_data.get("kpp"),
            "ogrn": company_data.get("ogrn"),
            "name": item.get("value"),
            "short_name": name_data.get("short_with_opf"),
            "full_name": name_data.get("full_with_opf"),
            "address": address_data.get("value"),
            "director_name": management.get("name"),
            "director_position": management.get("post"),
            "okved": company_data.get("okved"),
            "registration_date": state.get("registration_date"),
            "status": state.get("status"),
            "risk_score": 0,
        }