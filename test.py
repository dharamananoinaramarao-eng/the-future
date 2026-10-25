import argostranslate.translate

text = "Hello, how are you?"

translated = argostranslate.translate.translate(
    text,
    "en",
    "hi"
)

print("English:", text)
print("Hindi:", translated)