FROM ubuntu:16.04

COPY default_gatk3/ /usr/local/gatk3/
#COPY jre1.7.0_51/ /usr/local/gatk3/
COPY samt_default/bin /usr/local/samtools/

RUN apt-get update && \
    apt-get install -y default-jdk && \
    apt-get clean

RUN \
  apt-get update && \
  apt-get install -y python


#ENV GATK "/usr/local/gatk3/GenomeAnalysisTK.jar"
ENV GATK3_PATH "/usr/local/gatk3/"
ENV PATH "$GATK3_PATH:/usr/local/samtools:$PATH"

