FROM ubuntu:16.04
 
COPY bwa/ /usr/local/bwa_mem/bwa/0.7.12/
COPY samtools/ /usr/local/bwa_mem/samtools/1.2/
COPY sambamba/ /usr/local/bwa_mem/sambamba/v0.5.4/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/bwa_mem/bwa/0.7.12:$PATH"
 
ENV PATH /usr/local/bwa_mem/samtools/1.2/bin:$PATH"
 
ENV PATH /usr/local/bwa_mem/sambamba/v0.5.4:$PATH"
