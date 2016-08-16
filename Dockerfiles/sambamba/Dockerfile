FROM ubuntu:16.04

COPY samb_default/ /usr/local/sambamba/

RUN \
  apt-get update && \
  apt-get install -y python

ENV PATH "/usr/local/sambamba:$PATH"
