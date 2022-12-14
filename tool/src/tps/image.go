package tps

import (
	"bytes"
	"image"
	"image/color"
	"image/draw"
	"image/jpeg"
	_ "image/png"
	"log"
	"math"
	"os"
	"strings"
)

type Size struct {
	W int
	H int
}

type ImData struct {
	*image.RGBA
	image.Image
	Sz Size
}

/*
Init type ImData

Args:

	None

Returns:

	 type ImData struct {
		*image.RGBA
		image.Image
		Sz Size
	}
*/
func NewImData() *ImData {
	return &ImData{}
}

/*
Load ImData fron a file path

Args:

	string: path to image file

Returns:

	*ImData
*/
func (i *ImData) Load(p string) *ImData {
	bt, err := os.ReadFile(p)
	if err != nil {
		log.Panic(err)
	}
	info, _, err := image.DecodeConfig(bytes.NewReader(bt))
	if err != nil {
		log.Panic(err)
	}
	i.Sz = *NewSize(info.Width, info.Height)
	im, _, err := image.Decode(bytes.NewReader(bt))
	if err != nil {
		log.Panic(err)
	}
	i.Image = im
	i.ToRGBA()
	return i
}

/*
Transfor image.Image to image.RGBA

Args:

	None

Returns:

	None
*/
func (i *ImData) ToRGBA() {
	bounds := i.Image.Bounds()
	rgba := image.NewRGBA(bounds)
	draw.Draw(rgba, bounds, i.Image, bounds.Min, draw.Over)
	i.RGBA = rgba
}

/*
Save image in format=[png, jpg]

Args:

	p: save path

Returns:

	None
*/
func (i *ImData) Save(p string) {
	formats := [...]string{"png", "jpg"}
	strs := strings.Split(p, ".")
	sfx := strs[len(strs)-1]
	var confirmed bool = false
	for _, f := range formats {
		if sfx == f {
			confirmed = true
			break
		}
	}
	if !confirmed {
		log.Panic("Unsupported format:", sfx)
	}
	f, err := os.OpenFile(p, os.O_WRONLY|os.O_CREATE, 0755)
	if err != nil {
		log.Panic(err)
	}
	err = jpeg.Encode(f, i.RGBA, &jpeg.Options{Quality: 100})
	if err != nil {
		log.Panic(err)
	}
}

/*
Crop image

Args:

	*Rect:
		Xtl(float64): x of top-left point
		Ytl(float64): y of top-left point
		Xbr(float64): x of bottom-right point
		Ybr(float64): y of bottom-right point

Returns:

	*Image
*/
func (i *ImData) Crop(r *Rect) *ImData {
	ir := image.Rect(r.Xtl, r.Ytl, r.Xbr, r.Ybr)
	imD := NewImData()
	imD.Image = i.RGBA.SubImage(ir)
	imD.Sz = *NewSize(r.Xbr-r.Xtl+1, r.Ybr-r.Ytl+1)
	imD.ToRGBA()
	return imD
}

/*
Draw rectangle on image

Args:

	r *Rect: rectangle info [Xtl, Ytl, Xbr, Ybr]
	c color.Color: color setting

Returns:

	None
*/
func (i *ImData) DrawRect(r *Rect, c color.Color) {
	shorter := math.Min(float64(r.Xbr-r.Xtl), float64(r.Ybr-r.Ytl))
	bold := int(shorter / 100)
	if bold < 1 {
		bold = 1
	}
	or := image.Rect(r.Xtl, r.Ytl, r.Xbr, r.Ybr)
	imSub := i.Crop(r)
	min := imSub.Image.Bounds().Min
	max := imSub.Image.Bounds().Max
	for y := min.Y; y <= max.Y; y++ {
		for x := min.X; x <= max.X; x++ {
			if y < min.Y+bold || y >= max.Y-bold || x < min.X+bold || x >= max.X-bold {
				imSub.RGBA.Set(x, y, c)
			}
		}
	}
	draw.Draw(i.RGBA, or.Bounds(), imSub.RGBA, or.Min, draw.Over)
}
