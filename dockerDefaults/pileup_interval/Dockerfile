FROM ubuntu:16.04
 
COPY samtools/ /usr/local/pileup_interval/samtools/1.2/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/pileup_interval/samtools/1.2/bin:$PATH"
