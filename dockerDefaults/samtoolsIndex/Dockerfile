FROM ubuntu:16.04
 
COPY samtools/ /usr/local/samtoolsIndex/samtools/1.2/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/samtoolsIndex/samtools/1.2/bin:$PATH"
