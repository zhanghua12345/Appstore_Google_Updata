//
//  UIExtension.swift
//  AppleParty
//
//  Created by HTC on 2022/3/12.
//  Copyright © 2022 37 Mobile Games. All rights reserved.
//

import AppKit
import Foundation


extension NSAlert {
    
    static func show(_ content: String, title: String = "提示") {
        let alert = NSAlert()
        alert.messageText = title
        if content.count > 300 || content.contains("\n") {
            let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 560, height: 260))
            scrollView.hasVerticalScroller = true
            scrollView.hasHorizontalScroller = false
            scrollView.borderType = .bezelBorder

            let textView = NSTextView(frame: scrollView.bounds)
            textView.autoresizingMask = [.width]
            textView.isEditable = false
            textView.isSelectable = true
            textView.drawsBackground = false
            textView.textContainerInset = NSSize(width: 8, height: 8)
            textView.textContainer?.widthTracksTextView = true
            textView.textContainer?.containerSize = NSSize(width: scrollView.bounds.width, height: CGFloat.greatestFiniteMagnitude)
            textView.string = content
            textView.font = NSFont.systemFont(ofSize: 13)

            scrollView.documentView = textView
            alert.informativeText = "内容较长，请在下方滚动查看。"
            alert.accessoryView = scrollView
        } else {
            alert.informativeText = content
        }
        alert.runModal()
    }
}


extension NSImageView {
    func showWebImage(_ url: URL) {
        URLSession.shared.dataTask(with: url) { data, response, error in
            guard
                let httpURLResponse = response as? HTTPURLResponse, httpURLResponse.statusCode == 200,
                let mimeType = response?.mimeType, mimeType.hasPrefix("image"),
                let data = data, error == nil,
                let image = NSImage(data: data)
                else { return }
            DispatchQueue.main.async() { [weak self] in
                self?.image = image
            }
        }.resume()
    }
    
    func showWebImage(_ link: String) {
        guard let url = URL(string: link) else { return }
        showWebImage(url)
    }
}
