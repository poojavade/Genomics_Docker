FROM ubuntu:16.04

COPY bwa_default/ /usr/local/bwa/
COPY samt_default/bin /usr/local/samtools/
COPY samb_default/ /usr/local/sambamba/

RUN \
  apt-get update && \
  apt-get install -y python


ENV PATH "/usr/local/bwa:/usr/local/samtools:/usr/local/sambamba:$PATH"


