FROM ubuntu:16.04
 
COPY bwa/ /usr/local/bwa_mem_batch_friendly/bwa/0.7.12/
COPY samtools/ /usr/local/bwa_mem_batch_friendly/samtools/1.2/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/bwa_mem_batch_friendly/bwa/0.7.12:$PATH"
 
ENV PATH /usr/local/bwa_mem_batch_friendly/samtools/1.2/bin:$PATH"
