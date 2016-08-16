FROM ubuntu:16.04
 
COPY samtools/ /usr/local/bowtie2/samtools/1.2/
COPY sambamba/ /usr/local/bowtie2/sambamba/v0.5.4/
 
RUN \
  apt-get update && \
  apt-get install -y python
 
ENV PATH /usr/local/bowtie2/samtools/1.2/bin:$PATH"
 
ENV PATH /usr/local/bowtie2/sambamba/v0.5.4:$PATH"
