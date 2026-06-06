FROM python:3.13

RUN mkdir /app/
WORKDIR /app

COPY src/EuphemiaMarketClearing src/EuphemiaMarketClearing
COPY pyproject.toml ./
COPY README.md ./
RUN pip install ./

ENTRYPOINT python3 src/EuphemiaMarketClearing/euphemia_market_clearing.py