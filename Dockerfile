FROM python:3.12 AS py

FROM py AS build

RUN apt update

COPY requirements.txt /
RUN pip install --prefix=/inst -U -r /requirements.txt

FROM py

COPY --from=build /inst /usr/local

WORKDIR /geoguessr
CMD ["python", "main.py"]
COPY . /geoguessr
