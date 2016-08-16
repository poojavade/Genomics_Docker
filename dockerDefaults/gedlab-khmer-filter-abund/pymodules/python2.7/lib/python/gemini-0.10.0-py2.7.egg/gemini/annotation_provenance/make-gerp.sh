################
# GERP elements.
################
wget http://mendel.stanford.edu/SidowLab/downloads/gerp/hg19.GERP_elements.tar.gz

tar -zxvf hg19.GERP_elements.tar.gz

for chrom in `ls -1 hg19_*_elems.txt | cut -f2 -d "_"`
do
    # use the P-value (fifth column) as the BED score.
    awk -v chrom=$chrom '{print chrom"\t"$1"\t"$2"\t"$5}' "hg19_"$chrom"_elems.txt" >> tmp
done

sort -k1,1 -k2,2n tmp > hg19.gerp.elements.bed
rm tmp
rm hg19_*_elems.txt

bgzip hg19.gerp.elements.bed
tabix -p bed hg19.gerp.elements.bed.gz

############################
# GERP bp-resolution scores.
############################
wget http://hgdownload.cse.ucsc.edu/gbdb/hg19/bbi/All_hg19_RS.bw
mv All_hg19_RS.bw hg19.gerp.bw

