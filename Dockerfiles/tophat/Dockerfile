FROM ubuntu:16.04
 
COPY default/ /usr/local/tophat/
COPY samtools_default/bin/ /usr/local/samtools/
 

RUN \
  apt-get update && \
  apt-get install -y python

 
ENV PATH /usr/local/tophat:/usr/local/samtools:$PATH"
