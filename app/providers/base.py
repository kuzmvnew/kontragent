from abc import ABC, abstractmethod


class CompanyProvider(ABC):

    @abstractmethod
    def get_company(self, inn: str):
        pass