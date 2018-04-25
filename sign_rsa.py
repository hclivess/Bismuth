def sign():
    def verify_this():
        try:
            received_public_key = RSA.importKey(public_key_gui.get("1.0", END))
            verifier = PKCS1_v1_5.new(received_public_key)
            hash = SHA.new(input_text.get("1.0", END).encode("utf-8"))
            received_signature_dec = base64.b64decode(output_signature.get("1.0", END))

            if verifier.verify(hash, received_signature_dec):
                messagebox.showinfo ("Validation Result", "Signature valid")
            else:
                raise
        except:
            messagebox.showerror("Validation Result", "Signature invalid")